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
 * 超长段落按字符硬切，相邻块保留重叠以免答案被切在边界上。
 *
 * <p>与 Python 版的参数保持一致（600 字目标 / 80 字重叠 / 80 字下限）——
 * 两套系统共用同一个库，切分口径不同会导致同一篇文档在两边的检索结果对不上。
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

    private List<String> hardSplit(String block, int target, int lap) {
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
