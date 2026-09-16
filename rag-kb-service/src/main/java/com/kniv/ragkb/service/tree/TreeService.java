package com.kniv.ragkb.service.tree;

import com.kniv.ragkb.dao.mapper.TreeNodeMapper;
import com.kniv.ragkb.domain.entity.TreeNode;
import com.kniv.ragkb.service.config.RagProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 主题树的读取侧：给提示词生成「知识库覆盖了什么」的概览，以及按主题收窄检索范围。
 *
 * <p><b>为什么值得做</b>：没有这一层时，检索不到任何块就只能回一句
 * 「知识库中没有相关内容」—— 用户既不知道是「真的没有」还是「检索没召回到」，
 * 也不知道库里有哪些相近的方向。有了概览，回答可以先给出覆盖范围再落到细节。
 *
 * <p><b>收窄检索为什么默认关着</b>：聚类是按向量距离分的，它不懂语义。
 * 万一相关的块被分到了别的簇，收窄就等于把正确答案挡在门外 —— 而这失败是
 * 静默的（检索照样返回结果，只是漏了）。语料小的时候风险大于收益，
 * 所以默认 {@code narrowToClusters=0}（不收窄）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class TreeService {

    private final TreeNodeMapper mapper;
    private final RagProperties props;

    /** 树是否已建 */
    public boolean exists() {
        try {
            return !mapper.listAll().isEmpty();
        } catch (Exception e) {
            return false;
        }
    }

    public List<TreeNode> nodes() {
        try {
            return mapper.listAll();
        } catch (Exception e) {
            log.warn("读取主题树失败：{}", e.getMessage());
            return List.of();
        }
    }

    /**
     * 生成给提示词用的概览。
     *
     * <p>刻意写得短（默认 700 字上限）：它每轮都要出现，而上下文预算是
     * 和资料块共享的。一个主题一行，只给名字与一句话，细节留给检索。
     *
     * @return 概览文本；树没建或内容为空时返回 null
     */
    public String overview() {
        if (!props.getTree().isEnabled() || !props.getTree().isInjectOverview()) {
            return null;
        }
        List<TreeNode> nodes = nodes();
        if (nodes.isEmpty()) {
            return null;
        }
        StringBuilder sb = new StringBuilder();
        for (TreeNode n : nodes) {
            String line = "- " + n.getLabel() + "（" + n.getSize() + " 块）：" + n.getSummary();
            if (sb.length() + line.length() > props.getTree().getOverviewChars()) {
                break;
            }
            sb.append(line).append('\n');
        }
        return sb.isEmpty() ? null : sb.toString().strip();
    }

    /**
     * 按问题挑出最相关的几个主题簇，返回它们覆盖的块 id。
     *
     * <p>比的是<b>问题向量与簇质心</b>的距离，不是「问题与簇摘要文字」的距离：
     * 摘要是一句陈述，问题是一句提问，两者在向量空间里未必接近；
     * 而质心是簇内块向量的均值，本来就是为相似度匹配准备的。
     * 也不算「问题与簇内每一块」的距离 —— 那要把全库向量都捞进内存，
     * 而质心一个簇只有一个，比较是常数级的。
     *
     * @return 收窄用的块 id；不该收窄时返回 null
     */
    public Set<Long> narrowScope(float[] questionVector, int topClusters) {
        if (topClusters <= 0 || questionVector == null) {
            return null;
        }
        List<TreeNode> nodes = nodes();
        if (nodes.isEmpty()) {
            return null;
        }
        List<Map.Entry<TreeNode, Double>> scored = new ArrayList<>();
        for (TreeNode n : nodes) {
            float[] c = n.getCentroid();
            if (c == null || c.length != questionVector.length) {
                continue;
            }
            scored.add(Map.entry(n, cosineDistance(questionVector, c)));
        }
        if (scored.isEmpty()) {
            return null;
        }
        scored.sort(Map.Entry.comparingByValue());

        Set<Long> ids = new LinkedHashSet<>();
        for (int i = 0; i < Math.min(topClusters, scored.size()); i++) {
            Long[] chunkIds = scored.get(i).getKey().getChunkIds();
            if (chunkIds != null) {
                ids.addAll(java.util.Arrays.asList(chunkIds));
            }
        }
        return ids.isEmpty() ? null : ids;
    }

    /** 余弦距离，与 chunks 检索用的是同一个尺度，便于对照 */
    private static double cosineDistance(float[] a, float[] b) {
        double dot = 0, na = 0, nb = 0;
        for (int i = 0; i < a.length; i++) {
            dot += a[i] * b[i];
            na += a[i] * a[i];
            nb += b[i] * b[i];
        }
        if (na == 0 || nb == 0) {
            return 1.0;
        }
        return 1.0 - dot / (Math.sqrt(na) * Math.sqrt(nb));
    }

    /** 供状态展示：树是否过期 */
    public Map<String, Object> status(int currentChunks) {
        Map<String, Object> out = new LinkedHashMap<>();
        List<TreeNode> nodes = nodes();
        out.put("built", !nodes.isEmpty());
        out.put("clusters", nodes.size());

        Map<String, Object> meta = null;
        try {
            meta = mapper.meta();
        } catch (Exception e) {
            log.debug("读取建树元信息失败：{}", e.getMessage());
        }
        int builtChunks = meta == null || meta.get("chunk_count") == null
                ? 0 : ((Number) meta.get("chunk_count")).intValue();
        out.put("builtChunkCount", builtChunks);
        out.put("currentChunkCount", currentChunks);
        out.put("builtAt", meta == null ? null : String.valueOf(meta.get("built_at")));

        // 过期判定：块数变化超过两成，或新增超过 20 块。
        // 不用「一变就过期」—— 每加一篇文档都要重建的话没人会用
        int delta = currentChunks - builtChunks;
        boolean stale = !nodes.isEmpty()
                && (delta > 20 || (builtChunks > 0 && Math.abs(delta) > builtChunks * 0.2));
        out.put("stale", stale);

        List<Map<String, Object>> rows = new ArrayList<>();
        for (TreeNode n : nodes) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("label", n.getLabel());
            m.put("summary", n.getSummary());
            m.put("size", n.getSize());
            m.put("docs", n.getDocIds() == null ? 0 : n.getDocIds().length);
            rows.add(m);
        }
        out.put("topics", rows);
        return out;
    }
}
