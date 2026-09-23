package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * **议题 = 一个事件簇**（"谁在什么时候做了什么"），不是实体、也不是关键词。
 *
 * <p>用户 2026-09-24 定的粒度。理由：**补充/推翻发生在"某件事的说法"上** ——
 * 挂在实体上（某公司/某人）就退化成"这家的所有新闻"，时间线也就没了。
 *
 * <p>{@code embedding} 是**质心**（成员的均值）：它是**派生统计量**，与 append-only
 * 不冲突（append-only 管事实，不管统计）。维护见 {@code FeedTopicService}。
 *
 * <p>{@code firstSeen / lastSeen} 是议题的**时间轴**，也是"这件事在升温还是凉了"的原料 ——
 * 趋势题要的正是这个，而不是某一条新闻的相似度。
 */
@Data
@TableName("feed_topics")
public class FeedTopic {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String label;

    private String embedModel;

    private OffsetDateTime firstSeen;

    private OffsetDateTime lastSeen;

    private Integer itemN;
}
