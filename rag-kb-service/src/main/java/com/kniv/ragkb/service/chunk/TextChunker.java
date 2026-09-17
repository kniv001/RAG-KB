package com.kniv.ragkb.service.chunk;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/**
 * 文本切分：纯文本 → 分块列表。
 *
 * <p>策略：先按标题与空行切成段落保住语义边界，再把段落贪心打包到目标长度，
 * 超长段落**按句子边界**切，相邻块保留重叠以免答案被切在边界上。
 *
 * <p><b>为什么超长段落不能按字符硬切</b>：实测可复现「抽不出事实」的块，共同特征是
 * 从句子中间切开 —— 块首是「rk发生时…」这样的半句、块尾也断在半句上。半句开头的块
 * 指代无从解析、语义不完整，既拖累检索，也让模型在抽取时倾向放弃（同一段按句子边界
 * 对齐后，三次抽取都正常，且条数完全一致）。
 *
 * <p>与 Python 版的参数保持一致（600 字目标 / 80 字重叠 / 80 字下限）——
 * 两套系统共用同一个库，切分口径不同会导致同一篇文档在两边的检索结果对不上。
 * 注意：**边界规则这一版与 Python 版不同**，Python 版仍是字符硬切。
 */
@Data
@ConfigurationProperties(prefix = "ragkb.chunk")
@Component
public class TextChunker {

    /** 目标字符数。中文按字计，600 字约等于 400-500 token */
    private int size = 600;

    /** 相邻块的重叠字符数，避免答案恰好被切在边界 */
    private int overlap = 80;

    /** 小于此长度的块丢弃（通常是标题残留或空行） */
    private int minSize = 80;

    /** Markdown 标题行：强制独立成段，避免标题被并进正文段落 */
    private static final Pattern HEADING = Pattern.compile("^#{1,6}\\s+", Pattern.MULTILINE);

    public List<String> split(String text) {
        if (text == null || text.isBlank()) {
            return List.of();
        }
        int target = Math.max(size, 50);
        int lap = Math.min(overlap, target / 2);

        List<String> chunks = new ArrayList<>();
        StringBuilder buffer = new StringBuilder();

        for (String para : paragraphs(text)) {
            if (para.length() > target) {
                if (buffer.length() > 0) {
                    chunks.add(buffer.toString());
                    buffer.setLength(0);
                }
                chunks.addAll(hardSplit(para, target, lap));
                continue;
            }
            if (buffer.length() == 0) {
                buffer.append(para);
            } else if (buffer.length() + para.length() + 2 <= target) {
                buffer.append("\n\n").append(para);
            } else {
                chunks.add(buffer.toString());
                String tail = lap > 0 && buffer.length() > lap
                        ? buffer.substring(buffer.length() - lap) : "";
                buffer.setLength(0);
                if (!tail.isBlank()) {
                    buffer.append(tail).append("\n\n");
                }
                buffer.append(para);
            }
        }
        if (buffer.length() > 0) {
            chunks.add(buffer.toString());
        }

        List<String> out = new ArrayList<>(chunks.size());
        for (String c : chunks) {
            String t = c.strip();
            if (t.length() >= minSize) {
                out.add(t);
            }
        }
        // 全部块都因过短被丢弃时，至少保底返回原文，否则表现为「切分后无有效分块」
        if (out.isEmpty() && !chunks.isEmpty()) {
            for (String c : chunks) {
                if (!c.isBlank()) {
                    out.add(c.strip());
                }
            }
        }
        return out;
    }

    /** 按空行分段；标题行前强制插入空行，使其独立成段。 */
    private List<String> paragraphs(String text) {
        String withBreaks = HEADING.matcher(text).replaceAll(matchResult -> "\n\n" + matchResult.group());
        List<String> out = new ArrayList<>();
        for (String p : withBreaks.split("\n\\s*\n")) {
            String t = p.strip();
            if (!t.isEmpty()) {
                out.add(t);
            }
        }
        return out;
    }

    /**
     * 段落内切分：按**句子边界**打包，而不是按字符位置硬切。
     *
     * <p>先切句、再按目标长度贪心打包，相邻块之间带整句级别的重叠。
     * 只有单句本身超过目标长度时才退回字符硬切（代码块、无标点的长串）。
     *
     * <p>拼接所有句子**逐字等于原段落** —— 切分只改边界，不动内容。
     */
    private List<String> hardSplit(String block, int target, int lap) {
        List<String> out = new ArrayList<>();
        StringBuilder buffer = new StringBuilder();
        for (String sentence : sentences(block)) {
            if (sentence.length() > target) {
                if (buffer.length() > 0) {
                    out.add(buffer.toString());
                    buffer.setLength(0);
                }
                out.addAll(charSplit(sentence, target, lap));
                continue;
            }
            if (buffer.length() == 0) {
                buffer.append(sentence);
            } else if (buffer.length() + sentence.length() <= target) {
                buffer.append(sentence);
            } else {
                String piece = buffer.toString();
                out.add(piece);
                buffer.setLength(0);
                String tail = tailSentences(piece, lap);
                if (!tail.isBlank()) {
                    buffer.append(tail);
                }
                buffer.append(sentence);
            }
        }
        if (buffer.length() > 0) {
            out.add(buffer.toString());
        }
        return out;
    }

    /**
     * 切句：显式扫描，不在标点处切、只在句末标点与「代码行边界」处切。
     *
     * <p>标点留在前一句末尾，**拼接逐字等于输入**（切分只改边界，不动内容）。
     *
     * <p>为什么不把换行一律当边界：实测这个语料是网页抓来的，**75% 的行是硬折行**
     * （一句话被折成好几行），一律当边界等于把句子切碎，块首照样落在半句上。
     *
     * <p>为什么「一侧不含中文」就切：代码/输出行是不含中文的（抽样 40 篇里这类行占 75%），
     * 它们天然是独立的行单位；而中文散文即使折行，两侧都含中文，就不切。
     * 这条规则同时覆盖了 代码↔散文 的两种过渡。
     */
    private List<String> sentences(String block) {
        List<String> out = new ArrayList<>();
        int start = 0;
        int lineStart = 0;
        for (int i = 0; i < block.length(); i++) {
            char c = block.charAt(i);
            if (isSentenceEnd(c)) {
                out.add(block.substring(start, i + 1));
                start = i + 1;
                lineStart = i + 1;
                continue;
            }
            if (c == '\n') {
                int nextEnd = block.indexOf('\n', i + 1);
                if (nextEnd < 0) {
                    nextEnd = block.length();
                }
                boolean prevHasCjk = hasCjk(block, lineStart, i);
                boolean nextHasCjk = hasCjk(block, i + 1, nextEnd);
                if (!prevHasCjk || !nextHasCjk) {
                    out.add(block.substring(start, i + 1));
                    start = i + 1;
                }
                lineStart = i + 1;
            }
        }
        if (start < block.length()) {
            out.add(block.substring(start));
        }
        return out.isEmpty() ? List.of(block) : out;
    }

    private static boolean isSentenceEnd(char c) {
        return c == '。' || c == '！' || c == '？' || c == '!' || c == '?' || c == ';' || c == '；';
    }

    /** 区间 [from, to) 内是否含中日韩统一表意文字 */
    private static boolean hasCjk(String s, int from, int to) {
        for (int i = from; i < to && i < s.length(); i++) {
            char c = s.charAt(i);
            if (c >= 0x4E00 && c <= 0x9FFF) {
                return true;
            }
        }
        return false;
    }

    /**
     * 取末尾若干**整句**作为重叠部分，累计到 lap 字符为止。
     *
     * <p>为什么按整句而不是按字符：按字符截出来的开头又是半句，
     * 那就等于把刚修掉的问题从块尾搬到了块首。
     */
    private String tailSentences(String piece, int lap) {
        if (lap <= 0 || piece.length() <= lap) {
            return "";
        }
        List<String> ss = sentences(piece);
        StringBuilder tail = new StringBuilder();
        for (int i = ss.size() - 1; i >= 0 && tail.length() < lap; i--) {
            tail.insert(0, ss.get(i));
        }
        // 只剩一句时它就是整段，重叠无从谈起，直接不要（否则整段重复一遍）
        return tail.length() >= piece.length() ? "" : tail.toString();
    }

    /** 最后手段：单句超长（代码块、无标点长串）时按字符切 */
    private List<String> charSplit(String block, int target, int lap) {
        List<String> out = new ArrayList<>();
        int step = Math.max(1, target - lap);
        for (int start = 0; start < block.length(); start += step) {
            int end = Math.min(start + target, block.length());
            String piece = block.substring(start, end).strip();
            if (!piece.isEmpty()) {
                out.add(piece);
            }
            if (end >= block.length()) {
                break;
            }
        }
        return out;
    }
}
