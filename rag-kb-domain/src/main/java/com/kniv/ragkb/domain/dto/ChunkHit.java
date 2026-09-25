package com.kniv.ragkb.domain.dto;

import lombok.Data;

/**
 * 一次检索命中的分块。
 *
 * <p>{@code distance} 是余弦距离（越小越近）；关键词通道命中时它为 null，
 * 对应的 {@code hits} 才是有效分数。两种通道的分数不可直接比较 ——
 * 这正是混合检索要用 RRF（只看排名不看分数）融合的原因。
 */
@Data
public class ChunkHit {

    private Long id;

    private String docId;

    private String docName;

    private Integer seq;

    private String content;

    /**
     * 入库时为这块生成的**语境行**（一句「这段能回答什么问题」）。
     *
     * <p>它此前**只进检索索引、不进提示词** —— 那是 2026-09-18 的决定，理由是
     * "查询侧零开销"。但 2026-09-20 用 `latency-probe` 拆开计时之后发现：
     * 一次问答的 decode 里 **84~86% 的 token 是思考**，而思考的大头是
     * **逐条扫描全部召回块**（思考原文：「[1] 提到了…但没有…[2] 提到了…」，
     * 15 段扫一遍 ≈ 700 token）。
     *
     * <p>而把这一行随资料一起给模型，代价是 **prefill**（~3800 token/秒），
     * 省的是 **decode**（~75 token/秒）——**差约 50 倍**。所以"只进索引"那个决定
     * 值得重新量。
     */
    private String ctx;

    /** 余弦距离，仅向量通道有值 */
    private Double distance;

    /** 关键词命中数，仅关键词通道有值 */
    private Integer hits;

    /**
     * 来源信息，随检索结果一路带到回答的引用里。
     *
     * <p>网页内容会过期 —— 同一条结论，三年前抓的和昨天抓的可信度完全不同，
     * 而入库之后两者在检索结果里长得一模一样。引用时必须把抓取时间一并带出去，
     * 否则模型会把陈旧结论当成现行事实。
     */
    private String sourceKind;
    private String sourceUrl;
    private java.time.OffsetDateTime fetchedAt;

    /**
     * **这一次实际注入给模型的字**（分层注入时 = 块内挑中的那几句；空 = 整块注入）。
     *
     * <p>为什么它要一路带出去（2026-09-25）：判据要判"答案里的值有没有出处"，
     * 而它比对的**必须是模型真看到的那些字**。此前拿不到它，判据只好退回**整块正文** ——
     * 而分层注入只给块里挑中的几句（没挤进全局 top-M 的块才退回整块）
     * ⇒ 值可能"在块里、却从没进过提示词"，标它"该引没引"就是把**模型没有的机会
     * 算成它的失误**（错的方向是"多报"）。
     *
     * <p>⚠️ 量这个差值时踩过一次坑，记下来：第一版拿一趟**含 5/14 回放**的落盘结果量，
     * 得到"多报 36%"——那是**被回放污染的读数**（回放的答案是早先按**另一套选句**生成的，
     * 自然对不上现在的注入集）。**同一批答案**上重量的干净数字是
     * 整块 **8** / 真注入 **7**（`news-live` 清空答案缓存后的 14 题真跑）。
     * 教训：**"回放"与"真跑"不能混在一把尺子里** —— 这也是 `runner` 自己反复警告的那件事。
     *
     * <p>同样的理由对**提示词预算**也成立：那边早就在按"装什么估什么"算了
     * （见 {@code AgenticRagService} 里 `ctxTexts` 的注释），这里是把它**带出口**。
     */
    private String injected;

    public Double getScore() {
        return distance == null ? null : 1.0 - distance;
    }
}
