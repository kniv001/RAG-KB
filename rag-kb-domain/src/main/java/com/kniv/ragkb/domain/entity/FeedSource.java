package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * **信息流的来源**（一个域名一行）。
 *
 * <p>{@code authority} 是"来源于谁"那个权重，<b>只用于排序与准入，不用于判定真假</b> ——
 * 否则会变成"大媒体错了一起错"；真假由**证据聚合**（多少源、什么时间、是否被推翻）给出。
 *
 * <p>{@code totalN} / {@code refutedN} 是它被后续**推翻**的统计，用来校准 authority。
 * 这一对计数正是"append-only + 补充/推翻"模型给得起、而覆盖式存储给不起的东西：
 * 覆盖式存储里，被推翻的那条早就被改掉了，无从统计。
 *
 * <p>⚠️ 在本表上更新计数是**允许的**（统计量不是事实），但**条目本身一律 append-only**。
 */
@Data
@TableName("feed_sources")
public class FeedSource {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String domain;

    private String label;

    private Double authority;

    private Long totalN;

    private Long refutedN;

    private OffsetDateTime lastFetch;

    private OffsetDateTime createdAt;
}
