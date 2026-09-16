package com.kniv.ragkb.service.tree;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.TreeNodeMapper;
import com.kniv.ragkb.domain.entity.TreeNode;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.JsonExtract;
import com.kniv.ragkb.service.config.RagProperties;
import com.kniv.ragkb.service.index.EmbeddingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 建主题树：把全部块聚成若干主题簇，每簇生成一句概括。
 *
 * <p><b>聚类为什么自己做而不是调模型</b>：这一步是纯几何的（向量之间算距离），
 * 交给模型既慢又不准。模型只在最后一步出场 —— 给每个簇起名并概括。
 *
 * <p><b>为什么同一次聚类要固定随机种子</b>：k-means 的结果依赖初始化。
 * 不固定的话，同样的语料每次重建会得到不同的分簇，用户会觉得「我什么都没改，
 * 主题怎么全变了」。固定种子让结果可复现 —— 语料不变，树就不变。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class TreeBuildService {

    private static final String PROMPT = """
            你在为一个个人知识库做主题归并。下面是从库里聚出来的一组资料片段，
            请概括这一组讲的是什么。

            要求：
            1. label 是主题名，不超过 12 个字，要具体（写「缓存分层设计」，
               不要写「技术资料」这种放到哪都成立的词）。
            2. summary 说明这一组覆盖了什么，1~2 句，不超过 120 字。
            3. 若这一组内容很杂、看不出共同主题，label 就写「其他」，不要硬凑。
            4. 若给出了「已有主题」，而这一组其实就是其中某一个，label 就**照抄那个名字**
               —— 说明聚类把它和那个主题切成了两块，合并掉比另起一个近义名好得多。

            只输出 JSON：{"label":"...","summary":"..."}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"label":{"type":"string"},"summary":{"type":"string"}},
             "required":["label","summary"]}""";

    /** 固定种子：让同样的语料每次都聚出同样的结果 */
    private static final long SEED = 20260916L;

    private final TreeNodeMapper mapper;
    private final EmbeddingService embedding;
    private final ProviderRegistry providers;
    private final RagProperties props;
    private final ObjectMapper json;

    private final AtomicBoolean building = new AtomicBoolean(false);

    public boolean isBuilding() {
        return building.get();
    }

    public record Result(int chunks, int clusters, long ms) {
    }

    /**
     * 全量重建。同步执行 —— 个人规模的语料下几十秒内能完成。
     *
     * <p>重建期间用 {@code building} 挡住并发请求：两个建树同时在跑会互相覆盖，
     * 而结果是「一半新一半旧」的树，看起来正常但内容错乱。
     */
    public Result build() {
        RagProperties.Tree cfg = props.getTree();
        if (!cfg.isEnabled()) {
            throw new IllegalStateException("主题树未启用（ragkb.rag.tree.enabled=false）");
        }
        if (!building.compareAndSet(false, true)) {
            throw new IllegalStateException("正在建树，稍后再试");
        }
        long t0 = System.currentTimeMillis();
        try {
            String model = embedding.modelColumn();
            List<Map<String, Object>> rows = mapper.allVectors(model);
            if (rows.isEmpty()) {
                throw new IllegalStateException("库里还没有已索引的块，先上传或抓取一些资料");
            }

            List<Long> ids = new ArrayList<>(rows.size());
            List<String> contents = new ArrayList<>(rows.size());
            List<String> docIds = new ArrayList<>(rows.size());
            List<float[]> vectors = new ArrayList<>(rows.size());
            Map<String, String> docNames = new java.util.HashMap<>();
            for (Map<String, Object> r : rows) {
                ids.add(((Number) r.get("id")).longValue());
                contents.add(String.valueOf(r.get("content")));
                String did = String.valueOf(r.get("doc_id"));
                docIds.add(did);
                docNames.putIfAbsent(did, String.valueOf(r.get("doc_name")));
                vectors.add(VectorTypeConverter.parse(r.get("vec")));
            }

            int k = chooseK(vectors.size(), cfg);
            int[] assign = kmeans(vectors, k);

            // 按簇归拢
            List<List<Integer>> groups = new ArrayList<>();
            for (int c = 0; c < k; c++) {
                groups.add(new ArrayList<>());
            }
            for (int i = 0; i < assign.length; i++) {
                groups.get(assign[i]).add(i);
            }

            // 先归并再落库：聚类可能把同一个主题切成几块，而模型看得出来
            // （它会给同一个 label），合并掉比在地图里出现三行重名好得多
            List<String> usedLabels = new ArrayList<>();
            Map<String, List<Integer>> byLabel = new java.util.LinkedHashMap<>();
            Map<String, String> summaryByLabel = new java.util.LinkedHashMap<>();
            Map<String, float[]> centroidByLabel = new java.util.LinkedHashMap<>();
            int idx = 0;
            for (List<Integer> g : groups) {
                if (g.isEmpty()) {
                    continue;
                }
                float[] centroid = centroidOf(g, vectors);
                // label 为 null 表示「走兜底」：要么模型报错，要么它给的东西不能用。
                // summary 必须先初始化 —— summarize 抛异常时它不会被赋值，
                // 而兜底分支会把它补齐。
                String label = null;
                String summary = null;
                try {
                    String[] ls = summarize(g, contents, vectors, centroid, usedLabels);
                    label = ls[0];
                    summary = ls[1];
                    // 形状合法但内容是敷衍的（实测出现过 label="..."）也要拦下
                    if (!usable(label, 2) || !usable(summary, 8)) {
                        log.warn("第 {} 簇的概括不可用（label=「{}」，summary {} 字），改用兜底",
                                idx, label, summary.length());
                        label = null;
                    }
                } catch (Exception e) {
                    // 单个簇概括失败不该让整次建树白跑 —— 用兜底文案占位，
                    // 至少这一簇的块还能被检索到
                    log.warn("第 {} 簇概括失败：{}", idx, e.getMessage());
                    label = null;
                }
                if (label == null) {
                    // 兜底不写「其他」那种没信息量的词，而是把这一簇覆盖的文档名列出来 ——
                    // 至少能让用户看出这簇大概是些什么
                    label = "未归类主题 " + (idx + 1);
                    summary = "这一组内容较杂，模型的概括不可用。覆盖的文档："
                            + g.stream().map(i -> docIds.get(i))
                                    .collect(java.util.stream.Collectors.toCollection(LinkedHashSet::new))
                                    .stream().limit(3)
                                    .map(id -> docNames.getOrDefault(id, id))
                                    .collect(java.util.stream.Collectors.joining("、"));
                }

                String key = label.trim();
                if (!byLabel.containsKey(key)) {
                    usedLabels.add(key);
                    byLabel.put(key, new ArrayList<>());
                    summaryByLabel.put(key, summary);
                    centroidByLabel.put(key, centroid);
                } else {
                    // 同一个主题：质心取两者的平均，摘要保留先出现的那个
                    // （重名的簇本来就是同一主题的两半，摘要是同一件事的两种说法，
                    //  拼起来只会啰嗦）
                    float[] old = centroidByLabel.get(key);
                    for (int d = 0; d < old.length; d++) {
                        old[d] = (old[d] + centroid[d]) / 2f;
                    }
                }
                byLabel.get(key).addAll(g);
            }

            mapper.clear();
            idx = 0;
            for (Map.Entry<String, List<Integer>> e : byLabel.entrySet()) {
                List<Integer> g = e.getValue();
                TreeNode node = new TreeNode();
                node.setId("t" + (idx++) + "-" + Long.toHexString(System.currentTimeMillis()));
                node.setLabel(e.getKey());
                node.setSummary(summaryByLabel.get(e.getKey()));
                node.setChunkIds(g.stream().map(ids::get).toArray(Long[]::new));
                node.setCentroid(centroidByLabel.get(e.getKey()));
                node.setDocIds(g.stream().map(docIds::get).collect(
                        java.util.stream.Collectors.toCollection(LinkedHashSet::new)).toArray(String[]::new));
                node.setSize(g.size());
                mapper.insert(node);
            }
            int written = byLabel.size();
            mapper.saveMeta(vectors.size(), written);

            long ms = System.currentTimeMillis() - t0;
            log.info("主题树已重建：{} 块 → {} 个主题，耗时 {}ms", vectors.size(), written, ms);
            return new Result(vectors.size(), written, ms);
        } finally {
            building.set(false);
        }
    }

    // ---------------- 聚类 ----------------

    /**
     * 簇数的选择：约 √(n/2)，夹在 [2, maxClusters] 之间。
     *
     * <p>为什么不是固定值：块少的时候簇多了会把同一主题拆散，
     * 块多的时候簇少了又失去「收窄」的意义。√ 是个常用的经验尺度 ——
     * 100 块约 7 个簇，1000 块约 22 个（被上限截到 12）。
     */
    private int chooseK(int n, RagProperties.Tree cfg) {
        int k = (int) Math.round(Math.sqrt(n / 2.0));
        return Math.max(2, Math.min(k, Math.min(cfg.getMaxClusters(), n)));
    }

    /** k-means++ 初始化 + Lloyd 迭代。返回每一条的簇号。 */
    private int[] kmeans(List<float[]> data, int k) {
        int n = data.size();
        Random rnd = new Random(SEED);
        float[][] centroids = new float[k][];
        centroids[0] = data.get(rnd.nextInt(n));

        // k-means++：每个新质心按「离已有质心越远越可能被选中」挑，
        // 比纯随机初始化稳得多（随机初始化经常得到极不均衡的分簇）
        double[] dist = new double[n];
        for (int c = 1; c < k; c++) {
            double sum = 0;
            for (int i = 0; i < n; i++) {
                double d = Double.MAX_VALUE;
                for (int j = 0; j < c; j++) {
                    d = Math.min(d, sqDist(data.get(i), centroids[j]));
                }
                dist[i] = d;
                sum += d;
            }
            if (sum <= 0) {
                centroids[c] = data.get(rnd.nextInt(n));   // 全部重合，随便挑
                continue;
            }
            double target = rnd.nextDouble() * sum;
            int pick = n - 1;
            for (int i = 0; i < n; i++) {
                target -= dist[i];
                if (target <= 0) {
                    pick = i;
                    break;
                }
            }
            centroids[c] = data.get(pick);
        }

        int[] assign = new int[n];
        java.util.Arrays.fill(assign, -1);
        int dim = data.get(0).length;
        for (int iter = 0; iter < 30; iter++) {
            boolean changed = false;
            for (int i = 0; i < n; i++) {
                int best = 0;
                double bd = Double.MAX_VALUE;
                for (int c = 0; c < k; c++) {
                    double d = sqDist(data.get(i), centroids[c]);
                    if (d < bd) {
                        bd = d;
                        best = c;
                    }
                }
                if (assign[i] != best) {
                    assign[i] = best;
                    changed = true;
                }
            }
            float[][] next = new float[k][dim];
            int[] cnt = new int[k];
            for (int i = 0; i < n; i++) {
                int c = assign[i];
                float[] v = data.get(i);
                for (int d = 0; d < dim; d++) {
                    next[c][d] += v[d];
                }
                cnt[c]++;
            }
            for (int c = 0; c < k; c++) {
                if (cnt[c] == 0) {
                    next[c] = centroids[c];   // 空簇保持原位，下一轮可能重新吸引到点
                    continue;
                }
                for (int d = 0; d < dim; d++) {
                    next[c][d] /= cnt[c];
                }
            }
            centroids = next;
            if (!changed && iter > 0) {
                break;
            }
        }
        return assign;
    }

    private static double sqDist(float[] a, float[] b) {
        double s = 0;
        for (int i = 0; i < a.length; i++) {
            double d = a[i] - b[i];
            s += d * d;
        }
        return s;
    }

    // ---------------- 概括 ----------------

    /** 簇的质心（各成员向量的均值）。检索侧要用它判断问题落在哪个主题 */
    private float[] centroidOf(List<Integer> group, List<float[]> vectors) {
        float[] c = new float[vectors.get(0).length];
        for (int i : group) {
            float[] v = vectors.get(i);
            for (int d = 0; d < v.length; d++) {
                c[d] += v[d];
            }
        }
        for (int d = 0; d < c.length; d++) {
            c[d] /= group.size();
        }
        return c;
    }

    /** 取离质心最近的几条作为代表，交给模型起名概括 */
    private String[] summarize(List<Integer> group, List<String> contents,
                               List<float[]> vectors, float[] centroid, List<String> usedLabels) {
        List<Integer> byCentre = new ArrayList<>(group);
        byCentre.sort(java.util.Comparator.comparingDouble(i -> sqDist(vectors.get(i), centroid)));

        StringBuilder user = new StringBuilder("资料片段：\n");
        int take = Math.min(props.getTree().getSamplesPerCluster(), byCentre.size());
        for (int i = 0; i < take; i++) {
            String c = contents.get(byCentre.get(i)).replace('\n', ' ').strip();
            if (c.length() > 220) {
                c = c.substring(0, 220) + "…";
            }
            user.append('[').append(i + 1).append("] ").append(c).append('\n');
        }

        ProviderRegistry.Ref ref = providers.resolveChat(props.getAgent().getUtilityModel());
        String reply = providers.chatJson(ref,
                List.of(ChatMessage.system(PROMPT), ChatMessage.user(user.toString())),
                0.2, SCHEMA).content();

        JsonNode node = JsonExtract.parseObject(json, reply);
        String label = node == null ? "" : JsonExtract.string(node, "label", "");
        String summary = node == null ? "" : JsonExtract.string(node, "summary", "");
        return new String[]{label.strip(), summary.strip()};
    }

    /**
     * 检查模型给出的名字与概括是不是真的可用。
     *
     * <p>为什么要有这道校验：实测模型对某个「内容很杂、看不出共同主题」的簇
     * 返回了 {@code {"label":"...","summary":"..."}} —— 语法合法、形状正确，
     * 于是被当成成功。结果主题列表里出现一个叫「...」的主题，而它覆盖了 18 块。
     *
     * <p>提示词里已经写了「很杂就写『其他』」，但那只是请求。这里要求名字里
     * 至少有一个实义字符（汉字/字母/数字），否则判定为不可用、走兜底文案。
     */
    private static boolean usable(String s, int minLen) {
        if (s == null) {
            return false;
        }
        String t = s.strip();
        if (t.length() < minLen) {
            return false;
        }
        return t.codePoints().anyMatch(Character::isLetterOrDigit);
    }

    /** 把 @Select 拿回来的向量原文（pgvector 的文本形式）解成 float[] */
    private static final class VectorTypeConverter {
        static float[] parse(Object v) {
            if (v == null) {
                throw new IllegalStateException("块缺少向量，先重建索引");
            }
            return VectorTypeHandler.parse(String.valueOf(v));
        }
    }

    /** 库里已索引的块数，用于判断树是否过期 */
    public int indexedChunks() {
        try {
            return mapper.countIndexed(embedding.modelColumn());
        } catch (Exception e) {
            return 0;
        }
    }

    /** 供状态展示 */
    public Set<String> knownLabels() {
        Set<String> out = new LinkedHashSet<>();
        for (TreeNode n : mapper.listAll()) {
            out.add(n.getLabel());
        }
        return out;
    }
}
