package com.kniv.ragkb.service.feed;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * **去掉站点家具**的唯一实现 —— 晋升与富化**共用这一份**。
 *
 * <p><b>为什么必须有</b>（2026-09-25 实测，机制级证据）：界面新闻的「快讯」条目
 * 正文 482 字里，**真正的正文只有 77 字**（标题 23 + 一句话 54），其余全是模板：
 * <pre>
 *   金融快讯                                   ← 栏目名
 *   # WTI原油期货价格涨3%，逼近95美元/桶        ← 标题（唯一）
 *   界面快报 · 来源：界面新闻                   ← 站点标记（**每一条都一样**）
 *   WTI原油期货价格涨3%，报94.912美元/桶；…      ← 正文（一句话）
 *   未经正式授权严禁转载本文，侵权必究。          ← 声明（**每一条都一样**）
 *   点赞 / 收藏 / 看评论                        ← 交互件（**每一条都一样**）
 * </pre>
 * ⇒ **嵌入被模板主导**：19 条毫不相干的快讯（油价、美股、减持、罗永浩辟谣、习主席车队）
 * 互相相似度 **0.93+**，于是被议题聚类并成了一簇 —— 而它们的边相似度**比真正同一件事的
 * 那簇还高**（0.930 vs 0.937 中位；好簇反而 0.802~0.857）。
 * 换句话说：**这不是阈值问题，是"喂进去的东西 84% 是家具"**。
 *
 * <p><b>判据是跨条目的行频**，因为家具的定义就是"每一页都有"：出现在 ≥{@code minRepeats}
 * 个不同条目里的行就是家具。它**不需要任何站点知识**（不做站点适配），也**不需要逐条判断**。
 *
 * <p>⚠️ 两个边界（与 {@code FeedPromoteService} 里那份注释一致，别当成没有）：
 * <ol>
 *   <li>只统计/只作用于 **feed_items 内部**，**碰不到用户自己上传的文档**；</li>
 *   <li>同一条通稿被多家转载时，正文段落也可能撞成"高频" ⇒ 会被误删。
 *       但那种条目本来就会因 simhash 判重而不晋升（{@code dup_of IS NULL} 是前置条件）；
 *       富化那一步的后果是"向量少了一段"，比"整条并错簇"轻。</li>
 * </ol>
 */
public final class FeedText {

    /** 只统计"可能是家具"的长度区间：太短的（`|`、`#`）没信息，太长的几乎必然是正文段落。 */
    private static final int MIN_LEN = 2;

    private static final int MAX_LEN = 60;

    private FeedText() {
    }

    /** 行 → 它出现在多少个**不同条目**里。 */
    public static Map<String, Integer> lineFrequency(List<String> bodies) {
        Map<String, Integer> freq = new HashMap<>();
        for (String body : bodies) {
            if (body == null) {
                continue;
            }
            Set<String> seen = new HashSet<>();
            for (String raw : body.split("\r?\n")) {
                String l = raw.strip();
                if (l.length() < MIN_LEN || l.length() > MAX_LEN || !seen.add(l)) {
                    continue;
                }
                freq.merge(l, 1, Integer::sum);
            }
        }
        return freq;
    }

    /**
     * 把出现在 ≥{@code minRepeats} 个不同条目里的行删掉。
     *
     * <p>行**不删空行** —— 段落边界要留着，否则切分会把两段粘成一段。
     */
    public static String strip(String body, Map<String, Integer> freq, int minRepeats) {
        if (body == null || body.isBlank() || freq.isEmpty()) {
            return body;
        }
        StringBuilder out = new StringBuilder(body.length());
        for (String raw : body.split("\r?\n", -1)) {
            String l = raw.strip();
            if (l.length() >= MIN_LEN && l.length() <= MAX_LEN
                    && freq.getOrDefault(l, 0) >= minRepeats) {
                continue;
            }
            out.append(raw).append('\n');
        }
        return out.toString();
    }
}
