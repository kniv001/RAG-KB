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

    /**
     * **定时抓取总开关**，默认关。
     *
     * <p>两个理由：① 它会让服务端**周期性地**主动向外发请求，比"点一次抓一次"更该由人明确打开；
     * ② 第一版要先看一两轮的实际行为（抓了多少、什么被闸掉、议题分得对不对），
     * 而不是先让它自己跑起来。
     *
     * <p>⚠️ 它**不能**绕过 {@code ragkb.web.enabled}：那个关着时抓取一律不动（同一个出口）。
     */
    private boolean pollEnabled = false;

    /** 轮询间隔（毫秒）。默认 30 分钟 —— 新闻源的更新节奏大致如此，也更礼貌。 */
    private long pollIntervalMs = 1_800_000L;

    /**
     * **只抓 tier ≤ 这个值的源**。默认 **2**（国家级 + 门户/垂媒）。
     *
     * <p>这就是"按层级顺序推进"的落点：先国家级（最干净、最可核对），再放门户。
     * 2026-09-24 放到 2 —— 实测国家级只剩中新社一家在供货、门户层只剩界面新闻一家，
     * 两边都不够撑起一个方向，所以一起开。
     */
    private int tierMax = 2;

    /** **一轮的总抓取预算**（正文抓取条数）。默认 40：一轮 30 分钟，够用且不打扰对端。 */
    private int maxPerRun = 40;

    /** 单个频道一轮最多取几条。避免某个频道一次甩出 100 条把预算吃光。 */
    private int perChannelMax = 10;

    /**
     * **模板段的判定门槛**：同一段文字出现在 ≥ 这么多个**不同条目**里，就认定是站点家具。
     * 取 3 而不是 2：两家媒体恰好引同一句通稿是常事，三家以上才像模板。
     */
    private int boilerplateMinItems = 3;
}
