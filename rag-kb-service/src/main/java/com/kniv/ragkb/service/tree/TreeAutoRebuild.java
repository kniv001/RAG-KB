package com.kniv.ragkb.service.tree;

import com.kniv.ragkb.service.config.GpuGate;
import com.kniv.ragkb.service.config.RagProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * **主题树自动重建** —— 把"陈旧"这件事从**知道**变成**反应**。
 *
 * <p><b>为什么必须有它</b>（2026-09-24 实测）：新闻灌进知识库之后，主题树还停在
 * **09-17 的 661 块 / 10 个技术簇**，而那份概览**每轮都进提示词** ⇒
 * 模型拿一份**过时的全局视图**去否决**手上的资料**：
 * 一道题明明召回了 9 块、其中两块写着答案，它却答「知识库中没有……库覆盖的是技术方向、不含政治事件」。
 * 契约里**本来就写着**禁令（"资料在手就按【乙】答，别让概览推翻参考资料"）—— 那次没拦住。
 *
 * <p>而**过期判据早就有了**（{@code TreeService.status} 里的 {@code stale}：
 * 块数变化超过两成、或新增超过 20 块）—— 只是**没有任何一处消费它**。
 * 这个类就是那个消费者。
 *
 * <p><b>两个动作分开</b>：
 * <ol>
 *   <li>**发现陈旧就喊**（WARN）—— 无论重不重建。喊出来本身就有价值：
 *       一个"知道却不说"的字段等于没有；</li>
 *   <li>**确实陈旧才重建** —— 重建要调模型给每簇起名，所以要等 GPU 静默（与摘要/轮次笔记同一条规矩）。</li>
 * </ol>
 *
 * <p>⚠️ 重建是**同步**的（聚类是纯几何运算、只有起名调模型），十秒量级 ——
 * 所以它放在后台定时里没问题，但**不该在用户请求路径上触发**。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class TreeAutoRebuild {

    private final TreeService tree;
    private final TreeBuildService builder;
    private final GpuGate gpuGate;
    private final RagProperties props;

    /**
     * 每天跑一次（默认）。用 {@code fixedDelay}（上一轮结束再计时）而不是 {@code fixedRate}：
     * 重建时长不固定（要等 GPU 静默），fixedRate 会在慢轮里堆积。
     */
    @Scheduled(initialDelayString = "${ragkb.tree.auto-rebuild-initial-ms:300000}",
            fixedDelayString = "${ragkb.tree.auto-rebuild-ms:86400000}")
    public void tick() {
        // 用与建树**同一把尺子**数块（建树时数的就是「带向量的块」）。
        // 两处各数各的，就会出现「它说陈旧、它说不陈旧」这种对不上的读数。
        int current = builder.indexedChunks();
        Map<String, Object> st;
        try {
            st = tree.status(current);
        } catch (Exception e) {
            log.debug("主题树状态读不到：{}", e.getMessage());
            return;
        }
        boolean stale = Boolean.TRUE.equals(st.get("stale"));
        int built = ((Number) st.getOrDefault("builtChunkCount", 0)).intValue();
        if (!stale) {
            log.debug("主题树不陈旧（建树 {} 块 / 当前 {} 块）", built, current);
            return;
        }
        // ① **喊出来** —— 这一步不依赖任何开关，也不依赖能不能重建
        log.warn("⚠️ 主题树已陈旧：建树 {} 块 / 当前 {} 块（建于 {}）—— "
                        + "那份概览每轮都进提示词，陈旧会让模型拿旧全局视图去否决手上的资料",
                built, current, st.get("builtAt"));
        if (!props.getTree().isAutoRebuild()) {
            return;
        }
        if (builder.isBuilding()) {
            log.info("主题树正在重建中，这轮跳过");
            return;
        }
        // ② **确实重建** —— 起名要调模型，所以等用户静默（与摘要/轮次笔记同一条规矩）
        if (!gpuGate.awaitIdle()) {
            log.info("GPU 正忙（有用户请求在跑），主题树重建推到下一轮");
            return;
        }
        try {
            TreeBuildService.Result r = builder.build();
            log.info("主题树已自动重建：{} 块 / {} 簇（耗时 {} ms）", r.chunks(), r.clusters(), r.ms());
        } catch (Exception e) {
            log.warn("主题树自动重建失败（不影响问答，下轮再试）：{}", e.getMessage());
        }
    }
}
