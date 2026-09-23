package com.kniv.ragkb.service.agent;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * **引用规范化（后端拼装）**：把模型写出来的引用收敛成**契约形式** `[块号]`，
 * 句号那半截另存进结构化字段。
 *
 * <h3>为什么要有它</h3>
 *
 * 2026-09-23 连着两次实测到**同一件事**：
 * <ul>
 *   <li>给了地址 {@code ⟨2.4⟩} 的那一轮，模型在正文里写成 {@code [1.7]}</li>
 *   <li>**没给地址**的分层注入那一轮，它**自己编**了 {@code [4.3]}、{@code [2.11]}</li>
 * </ul>
 * ⇒ <b>只要注入的单位比"块"更细，模型就自发按句子粒度引用。</b>
 * 而那些引用**逐条核对全是对的**（指的是支持该句答案的那句原文）。
 *
 * <p>问题在于两处消费方都只认 `[n]`：判据的正则 {@code \[(\d{1,2})\]}
 * （`]` 前多了 `.3` 就匹配不到 ⇒ **一条有效引用在判据眼里变成"缺引用"**），
 * 以及前端渲染（`[n]` 才渲染成跳来源的链接）。于是好答案被判成坏的。
 *
 * <h3>做法：模型尽管写，后端收敛</h3>
 *
 * 这是本项目反复用过的分工（覆盖边、数字判据都是"模型做不好的，代码机械补"）：
 * <b>不跟模型的能力对抗，也不改判据去迁就它</b>，而是在中间做一次规范化 ——
 * 于是**判据、前端、历史数字一概不动**。
 *
 * <p>句子号不丢：进 {@link #cites()}（形如 {@code "4.3,2.11"}），随 STATS 事件出去，
 * 将来前端要做"第 4 段第 3 句"的高亮时现成就有。
 *
 * <h3>为什么带缓冲</h3>
 *
 * 正文是**流式**吐出来的，`[4` 和 `.3]` 可能分在两个 token 里 —— 不缓冲就没法判断。
 * 规则：见到 `[`/`⟨` 开始攒，见到配对的 `]`/`⟩` 收口；
 * **攒超过 {@link #MAX} 个字符还没收口就原样放出去**（那多半不是引用，是 markdown 链接或正文里的方括号）。
 */
final class CiteFix {

    /** 引用的最长可能长度（`[12.34]` 才 7 个字符，留足余量）。 */
    private static final int MAX = 12;

    /** 句子级：`[4.3]` / `⟨4.3⟩` / `[4．3]`（全角点）。 */
    private static final Pattern SENT = Pattern.compile("[\\[⟨]\\s*(\\d{1,2})\\s*[.．]\\s*(\\d{1,2})\\s*[\\]⟩]");

    private final StringBuilder hold = new StringBuilder();
    /** 每条 {段号, 句号, **在正文里的偏移**} —— 偏移是给判据用的：
     *  "这个引用挂在哪句结论上"要靠它，否则只能知道"引过第 4 段第 3 句"，
     *  没法判那句话支不支持它所挂的那条结论。 */
    private final List<int[]> cites = new ArrayList<>();
    private boolean holding = false;
    /** 已经发出去的字数 —— 偏移按它算（与判据看到的正文下标一致）。 */
    private int emitted = 0;

    /** 吃一段流式文本，返回**已经可以安全发出去**的文本（末尾可能是空的）。 */
    String feed(String piece) {
        StringBuilder out = new StringBuilder(piece.length() + 8);
        for (int i = 0; i < piece.length(); i++) {
            char ch = piece.charAt(i);
            if (!holding && (ch == '[' || ch == '⟨')) {
                holding = true;
                hold.setLength(0);
                hold.append(ch);
                continue;
            }
            if (holding) {
                hold.append(ch);
                char close = hold.charAt(0) == '[' ? ']' : '⟩';
                if (ch == close) {
                    out.append(resolve(hold.toString()));
                    holding = false;
                } else if (hold.length() > MAX) {
                    // 不像引用 —— 原样放出去，别把正文吞了
                    out.append(hold);
                    holding = false;
                }
                continue;
            }
            out.append(ch);
        }
        emitted += out.length();
        return out.toString();
    }

    /** 正文结束时把残留放出去（**不能吞字**：宁可格式差，不能少内容）。 */
    String flush() {
        if (!holding) {
            return "";
        }
        holding = false;
        String s = hold.toString();
        hold.setLength(0);
        return s;
    }

    private String resolve(String raw) {
        Matcher m = SENT.matcher(raw);
        if (!m.matches()) {
            return raw;                      // 不是句子级引用 ⇒ 原样（块级 [n] 也走这里）
        }
        int n = Integer.parseInt(m.group(1));
        int s = Integer.parseInt(m.group(2));
        // 偏移 = 已发出的字数（`[n]` 就替换在这一位上，两者对齐）
        cites.add(new int[]{n, s, emitted});
        // **只留块号** —— 判据与前端认的就是这个。句号与偏移进了 cites，不丢。
        return "[" + n + "]";
    }

    /** 规范化过的句子级引用，形如 `4.3@123,2.11@456`（空则空串）。
     *
     *  {@code @} 后面是**在正文里的字符偏移** —— 判据靠它找到"这个引用挂在哪句结论上"，
     *  才能判那句话支不支持它所引的那一句。没有偏移就只能判"指得到"，判不了"指得对"。 */
    String citeStr() {
        StringBuilder b = new StringBuilder();
        for (int[] c : cites) {
            if (b.length() > 0) {
                b.append(',');
            }
            b.append(c[0]).append('.').append(c[1]).append('@').append(c[2]);
        }
        return b.toString();
    }

    int count() {
        return cites.size();
    }
}
