package com.kniv.ragkb.service.feed;

import java.util.ArrayList;
import java.util.List;

/**
 * **粗糙切分**：把一篇正文切成"段"，只为回答一个问题 —— *这一片内容库里是不是已经有了*。
 *
 * <p>刻意**不**做成检索单元：那个角色是 `chunks` 的（有 markdown 感知、有逐字还原的约束、
 * 有语料戳）。这里要的是**够粗、够快、能定位**：切得细会把"一整段通稿"拆散、判不出重复；
 * 切得粗会把不同内容混在一段里、向量被平均掉。所以取"**段落优先、超长再按句切、过短就合并**"，
 * 目标长度 60~300 字。
 *
 * <p>⚠️ 与 `tools/sentence_split.py`（句子索引那把）的分工：那个要求**逐字还原原文**，
 * 因为它产出的句子要能按字符区间拼回正文；这里**不要求还原**（段间空白会被吃掉）——
 * 它只用来比对，不参与引用。
 */
public final class FeedSegmenter {

    /** 目标区间。低于下限就与邻段合并，高于上限就按句切。 */
    private static final int MIN_CHARS = 60;
    private static final int MAX_CHARS = 300;

    private FeedSegmenter() {
    }

    /** 一片段落：文本 + 在原文里的字符区间（区间用来**回溯**到正文，不用于还原）。 */
    public record Seg(String text, int start, int end) {
    }

    /**
     * 切分。输入是已抽取的正文（Markdown 或纯文本）。
     *
     * <p>三步：按空行切成块 → **短块先攒够再切** → 超长的按句读点切。
     *
     * <p>⚠️ 中间那步一开始漏了，代价量化过：一篇 3104 字的门户页切出 **255 段**（平均 12 字）
     * —— 导航页的碎行各自成段，于是"随机两个碎行也很像"（"首页" ↔ "首页"），
     * 重复率被抬到 31% 那种假读数上。**短段是这套方法最脏的噪声源**，必须攒够再切。
     *
     * <p>Markdown 的代码围栏与表格行不特殊处理 —— 判断重复对它们同样有效，
     * 而特殊处理只会让"哪些区间覆盖了原文"这件事变复杂。
     */
    public static List<Seg> split(String body) {
        List<Seg> out = new ArrayList<>();
        if (body == null || body.isBlank()) {
            return out;
        }
        int n = body.length();
        StringBuilder cur = new StringBuilder();
        int curStart = 0;
        int curEnd = 0;

        for (int[] block : blocks(body, n)) {
            int bs = block[0];
            int be = block[1];
            if (cur.isEmpty()) {
                curStart = bs;
            }
            if (!cur.isEmpty()) {
                cur.append('\n');
            }
            cur.append(body, bs, be);
            curEnd = be;
            // 攒够了下限才切；不够就继续并下一块 —— 短段是噪声，不是更细的粒度
            if (cur.length() >= MIN_CHARS) {
                flush(out, cur, curStart, curEnd);
            }
        }
        if (!cur.isEmpty()) {
            flush(out, cur, curStart, curEnd);
        }
        return out;
    }

    /** 按空行切成块，返回每块的 [起, 止)（已去掉首尾空白）。 */
    private static List<int[]> blocks(String body, int n) {
        List<int[]> out = new ArrayList<>();
        int i = 0;
        int start = 0;
        while (i < n) {
            if (isBlankLineAt(body, i)) {
                addBlock(out, body, start, i);
                int skip = i;
                while (skip < n && isSpace(body.charAt(skip))) {
                    skip++;
                }
                i = skip;
                start = i;
                continue;
            }
            i++;
        }
        addBlock(out, body, start, n);
        return out;
    }

    private static void addBlock(List<int[]> out, String body, int start, int end) {
        int s = start;
        int e = end;
        while (s < e && isSpace(body.charAt(s))) {
            s++;
        }
        while (e > s && isSpace(body.charAt(e - 1))) {
            e--;
        }
        if (e > s) {
            out.add(new int[]{s, e});
        }
    }

    private static boolean isSpace(char c) {
        return c == ' ' || c == '\t' || c == '\n' || c == '\r';
    }

    private static void flush(List<Seg> out, StringBuilder cur, int start, int end) {
        String text = cur.toString().strip();
        cur.setLength(0);
        if (text.isEmpty()) {
            return;
        }
        if (text.length() <= MAX_CHARS) {
            out.add(new Seg(text, start, end));
            return;
        }
        // 超长：按句读点切成几片，各自带上（估算的）区间
        for (String piece : splitSentences(text)) {
            out.add(new Seg(piece, start, end));
        }
    }

    /** 按中文句读点切；切完若是碎片（&lt; MIN_CHARS）就并进前一片，避免造出一堆"半句话"。 */
    private static List<String> splitSentences(String text) {
        List<String> parts = new ArrayList<>();
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            b.append(c);
            if (c == '。' || c == '！' || c == '？' || c == '；' || c == '\n'
                    || (c == '.' && i + 1 < text.length() && text.charAt(i + 1) == ' ')) {
                if (b.length() >= MIN_CHARS) {
                    parts.add(b.toString().strip());
                    b.setLength(0);
                }
            }
        }
        if (!b.isEmpty()) {
            parts.add(b.toString().strip());
        }
        // 合并：让每一片尽量落在 [MIN, MAX] 里
        List<String> merged = new ArrayList<>();
        StringBuilder acc = new StringBuilder();
        for (String p : parts) {
            if (acc.isEmpty()) {
                acc.append(p);
                continue;
            }
            if (acc.length() + p.length() + 1 <= MAX_CHARS) {
                acc.append(' ').append(p);
            } else {
                merged.add(acc.toString());
                acc.setLength(0);
                acc.append(p);
            }
        }
        if (!acc.isEmpty()) {
            merged.add(acc.toString());
        }
        return merged;
    }

    private static boolean isBlankLineAt(String s, int i) {
        // 一个 \n 后面只跟空白，再来一个 \n —— 即两个换行之间没有非空白字符
        if (s.charAt(i) != '\n') {
            return false;
        }
        int j = i + 1;
        while (j < s.length() && (s.charAt(j) == ' ' || s.charAt(j) == '\t' || s.charAt(j) == '\r')) {
            j++;
        }
        return j < s.length() && s.charAt(j) == '\n';
    }
}
