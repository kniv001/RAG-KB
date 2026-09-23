package com.kniv.ragkb.service.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * 信息流（`news` 分支）的参数。
 *
 * <p>⚠️ **两个相似度阈值都是临时值，等校准** —— 它们的依据不是拍脑袋，而是
 * `feed_segments.best_sim` 与 `feed_items.nearest_topic_sim` 两列记下来的分布。
 * 这两列**就是为校准这两个数而存在的**：先跑一批，看分布，再定阈值。
 * 这与上面 `Simhash.DUP_DISTANCE` 是同一个规矩（那边由 `nearest_dist` 校准）。
 */
@Data
@ConfigurationProperties(prefix = "ragkb.feed")
@Component
public class FeedProperties {

    /**
     * **段落判"已有"的相似度阈值**：一段内容与库里已有段落的余弦相似度超过它，
     * 就算"这片已经有了"。0.92 是临时的 —— bge-m3 上逐字相同通常 &gt;0.97，
     * 改写过的通稿段大概落在 0.88~0.95，所以这个数会把"轻度改写"也算成重复。
     * **要按 best_sim 的分布调**。
     */
    private double dupSim = 0.92;

    /**
     * **dup_ratio 超过它就把条目降为冷存**（不召回）。默认 0.8：一整篇八成内容
     * 库里都有，那它对召回的边际贡献很小 —— 但**行仍然留着**（可见性判断，不是存亡判断）。
     */
    private double dupRatioDemote = 0.8;

    /**
     * **并入议题的相似度阈值**（与议题质心的余弦）。0.80 是临时的，
     * 按 `nearest_topic_sim` 的分布调。
     */
    private double topicSim = 0.80;

    /**
     * **议题的时间窗（天）**。新闻的事件有寿命：不设窗的话"金价"会把半年前的另一件事
     * 并进来，议题就退化成主题（主题是另一层的东西）。
     */
    private int topicWindowDays = 7;

    /** 一次富化批最多处理几条。**显式上限**：后台任务一次占太久会拖住用户请求。 */
    private int enrichLimit = 30;
}
