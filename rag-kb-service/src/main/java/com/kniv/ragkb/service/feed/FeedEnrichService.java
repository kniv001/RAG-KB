package com.kniv.ragkb.service.feed;

import com.kniv.ragkb.dao.mapper.FeedIndexMapper;
import com.kniv.ragkb.domain.entity.FeedTopic;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import com.kniv.ragkb.service.config.FeedProperties;
import com.kniv.ragkb.service.config.GpuGate;
import com.kniv.ragkb.service.index.EmbeddingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * **信息流的富化**（第 1、2 层）：一段代码切分 + 一次向量模型，两件事一起做完。
 *
 * <pre>
 *   条目正文 ──粗糙切分──▶ 段 ──嵌入──▶ 逐段"库里有没有" ──▶ dup_seg_n / seg_n
 *                                    └──前几段均值──▶ 与议题质心比 ──▶ 并入 or 新建
 * </pre>
 *
 * <p><b>这个形状是用户 2026-09-24 提的</b>："代码先进行一次粗糙的预处理切分，然后再进行
 * 向量模型的回归测试，如果大量回归就是重复的片段。" 它补上了整篇 simhash 的盲区 ——
 * 实测同一件事的三家报道**整篇距离 28/64**（各家自己写），但**段落**仍可能大段相同
 * （通稿段被抄）。所以：整篇指纹抓逐字转载，段落回归抓"部分抄 + 改写"。
 *
 * <p><b>共用同一次 embedding</b>：段向量既用于重复判定，前几段的均值又当条目的表示
 * 去比议题 —— 层数增加不等于 GPU 成本成倍增加。这是能把三层都做起来的前提。
 *
 * <p>⚠️ <b>单线程</b>：议题质心是"读出来 → 混合 → 写回"，并发跑会互相覆盖。
 * 这里用一个显式的单线程池串行化（与 {@code SummaryService} 同一个理由：
 * 后台任务不该和用户请求抢推理槽，也不该自己抢自己）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class FeedEnrichService {

    private final FeedIndexMapper idx;
    private final EmbeddingService embedding;
    private final GpuGate gpuGate;
    private final FeedProperties props;

    /** 一条条目的富化结果。`dupRatio` 是**这一支最关键的读数**：
     *  "这篇有多少比例的段落是库里已有的"。 */
    public record Enriched(long id, int segN, int dupSegN, double dupRatio,
                           Float topicSim, Long topicId, boolean newTopic, String note) {
    }

    public record Batch(int pending, int done, int newTopics, int demoted, List<Enriched> items,
                        String note) {
    }

    /**
     * 跑一批。**显式传 limit**，不自动循环到底 —— 后台任务一次占太久会拖住用户请求
     * （本机只有一个推理槽，实测后台跑 3091ms 用户就要等 3095ms）。
     */
    public synchronized Batch run(int limit) {
        List<Map<String, Object>> pending = idx.pendingEnrich(limit);
        if (pending.isEmpty()) {
            return new Batch(0, 0, 0, 0, List.of(), "没有待富化的条目");
        }
        // 等用户静默再开始 —— 与摘要/轮次笔记走同一个闸
        if (!gpuGate.awaitIdle()) {
            return new Batch(pending.size(), 0, 0, 0, List.of(), "GPU 正忙（有用户请求在跑），这次不抢");
        }

        List<Enriched> out = new ArrayList<>();
        int newTopics = 0;
        int demoted = 0;
        for (Map<String, Object> row : pending) {
            long id = ((Number) row.get("id")).longValue();
            try {
                Enriched e = one(id, str(row.get("title")), str(row.get("body")));
                out.add(e);
                if (e.newTopic()) {
                    newTopics++;
                }
                if (e.dupRatio() >= props.getDupRatioDemote()) {
                    demoted++;
                }
            } catch (Exception ex) {
                log.warn("富化失败 #{}：{}", id, ex.getMessage());
            }
        }
        log.info("信息流富化：{} 条（新建议题 {} · 判为高度重复 {}）", out.size(), newTopics, demoted);
        return new Batch(pending.size(), out.size(), newTopics, demoted, out, null);
    }

    /** 议题榜（按条目数）——"哪件事在升温"最粗的一个视图。 */
    public List<Map<String, Object>> topTopics(int limit) {
        return idx.topTopics(limit);
    }

    private Enriched one(long id, String title, String body) {
        List<FeedSegmenter.Seg> segs = FeedSegmenter.split(body);
        if (segs.isEmpty()) {
            idx.updateEnriched(id, VectorTypeHandler.toLiteral(new float[1024]),
                    embedding.modelColumn(), 0, 0, null);
            return new Enriched(id, 0, 0, 0, null, null, false, "切不出段落（正文过短或全空白）");
        }

        // ① 一次批量嵌入全部段
        List<float[]> vecs = embedding.embed(segs.stream().map(FeedSegmenter.Seg::text).toList(), null);
        String model = embedding.modelColumn();

        // ② 逐段"回归测试"：这一段库里有没有？（同一篇除外；只比可召回的条目）
        int dup = 0;
        for (int i = 0; i < segs.size(); i++) {
            Float best = null;
            try {
                String lit = VectorTypeHandler.toLiteral(vecs.get(i));
                List<Double> hit = idx.nearestSegmentSim(lit, model, id);
                if (!hit.isEmpty() && hit.get(0) != null) {
                    best = hit.get(0).floatValue();
                    if (best >= props.getDupSim()) {
                        dup++;
                    }
                }
                idx.insertSegment(id, i, segs.get(i).text(), segs.get(i).start(), segs.get(i).end(),
                        lit, model, best);
            } catch (Exception e) {
                log.warn("段落 {} 处理失败：{}", i, e.getMessage());
            }
        }
        double ratio = (double) dup / segs.size();

        // ③ 条目表示 = **前几段向量的均值**（近似导语，又不至于被单个坏段带偏）
        float[] lead = mean(vecs, Math.min(3, vecs.size()));

        // ④ 议题归并
        Long topicId = null;
        Float topicSim = null;
        boolean isNew = false;
        try {
            String lit = VectorTypeHandler.toLiteral(lead);
            List<Map<String, Object>> near = idx.nearestTopic(lit, model, props.getTopicWindowDays());
            if (!near.isEmpty()) {
                topicSim = ((Number) near.get(0).get("sim")).floatValue();
            }
            if (!near.isEmpty() && topicSim != null && topicSim >= props.getTopicSim()) {
                Map<String, Object> t = near.get(0);
                topicId = ((Number) t.get("id")).longValue();
                int n = ((Number) t.get("itemN")).intValue();
                // 质心：新均值 = (旧均值 × n + 本条) / (n+1)
                float[] old = parseVector(str(t.get("emb")));
                float[] merged = blend(old, n, lead);
                idx.updateTopicCentroid(topicId, VectorTypeHandler.toLiteral(merged), n + 1);
                idx.linkItemTopic(id, topicId, topicSim);
            } else {
                FeedTopic t = new FeedTopic();
                t.setLabel(null);   // 标签留给模型（闸 2 那一步）；先不猜
                idx.insertTopic(t, lit, model);
                topicId = t.getId();
                idx.linkItemTopic(id, topicId, 1.0f);
                isNew = true;
            }
        } catch (Exception e) {
            log.warn("议题归并失败 #{}：{}", id, e.getMessage());
        }

        idx.updateEnriched(id, VectorTypeHandler.toLiteral(lead), model, segs.size(), dup, topicSim);
        return new Enriched(id, segs.size(), dup, ratio, topicSim, topicId, isNew, null);
    }

    private static float[] mean(List<float[]> vs, int n) {
        if (n <= 0) {
            return new float[1024];
        }
        float[] out = new float[vs.get(0).length];
        for (int i = 0; i < n; i++) {
            float[] v = vs.get(i);
            for (int j = 0; j < out.length; j++) {
                out[j] += v[j];
            }
        }
        for (int j = 0; j < out.length; j++) {
            out[j] /= n;
        }
        return out;
    }

    /** 加权混合：(旧 × n + 新) / (n+1)。 */
    private static float[] blend(float[] old, int n, float[] add) {
        int len = Math.min(old.length, add.length);
        float[] out = new float[len];
        for (int j = 0; j < len; j++) {
            out[j] = (old[j] * n + add[j]) / (n + 1);
        }
        return out;
    }

    /** pgvector 的文本形如 {@code [0.1,0.2,...]}。 */
    private static float[] parseVector(String text) {
        if (text == null || text.length() < 3) {
            return new float[1024];
        }
        String[] parts = text.substring(1, text.length() - 1).split(",");
        float[] out = new float[parts.length];
        for (int i = 0; i < parts.length; i++) {
            out[i] = Float.parseFloat(parts[i].trim());
        }
        return out;
    }

    private static String str(Object o) {
        return o == null ? "" : o.toString();
    }
}
