package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * **信息流的一个条目**：抓到就落库，**可见性由 {@code status} 决定，行一律留下**。
 *
 * <p>与 {@code documents} 并存不替代：这一张是"抓到的东西"的落点，
 * {@code documents} 是"值得进知识库的东西"—— 由这里**过闸且不重复**的转过去。
 * 分开的理由见 {@code FeedGate} 的类注释（丢弃不可逆 + 要留样本校准权重）。
 *
 * <p>{@code dupOf} 指向**首见**条目（不是上一跳）：一条新闻被转三次时，
 * 三条都指回最早那条，而不是串成链 —— 否则"这个事件有几条报道"要递归才能数出来。
 *
 * <p>⚠️ 时间维度的态度：新信息对旧信息是**补充或推翻，不存在覆盖**。
 * 所以这张表只会被 INSERT；{@code published_at} 与 {@code fetched_at} 是两个不同的时间
 * （事件时间 vs 记录时间），**都要留**：只留一个的话，"当时我们以为是什么"就答不出来。
 */
@Data
@TableName("feed_items")
public class FeedItem {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long sourceId;

    private String url;

    private String title;

    private String body;

    private OffsetDateTime publishedAt;

    private OffsetDateTime fetchedAt;

    /** 64 位 simhash（3-gram shingle 加权），近重复判定的载体。 */
    private Long simhash;

    /** 近重复指向**首见**条目；NULL = 本条是首见。 */
    private Long dupOf;

    /** 0=冷存不召回 · 1=可召回 · 2=已入库为文档 */
    private Integer status;

    /** 判定理由：不记的话"为什么这条没召回"只能靠猜。 */
    private String gateReason;

    /**
     * 与**最近候选**的汉明距离（NULL = 连候选都没有）。
     *
     * <p>这一列是**校准阈值的仪器**，不是判定结果：{@code DUP_DISTANCE=3} 是沿用经典取值，
     * 而"转载"的真实距离分布得从数据里看。⚠️ 它只统计 **LSH 候选里**的最近距离 ——
     * 没候选时无从知道真正的最近是多少，读的时候要带上这个边界。
     */
    private Integer nearestDist;

    private Integer charN;

    private OffsetDateTime createdAt;
}
