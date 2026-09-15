package com.kniv.ragkb.service.retrieve;

import com.kniv.ragkb.dao.mapper.ChunkMapper;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import com.kniv.ragkb.service.config.RagProperties;
import com.kniv.ragkb.service.index.EmbeddingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 检索：问题 → 相关分块。三种模式，默认混合。
 *
 * <p>混合检索用 RRF（Reciprocal Rank Fusion）而非加权求和：向量通道给的是余弦距离
 * （0~2 的连续量），关键词通道给的是命中次数（整数，量纲完全不同）。
 * 把两者归一化再加权，等于人为编造一个不存在的可比性；RRF 只看排名不看分数，
 * 天然免疫这个问题。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class Retriever {

    public static final String MODE_VECTOR = "vector";
    public static final String MODE_KEYWORD = "keyword";
    public static final String MODE_HYBRID = "hybrid";

    private static final Pattern CJK = Pattern.compile("[\\u4e00-\\u9fff]");
    private static final Pattern WORD = Pattern.compile("[A-Za-z0-9_]{2,}");
    private static final int RRF_K = 60;

    private final ChunkMapper chunkMapper;
    private final EmbeddingService embedding;
    private final RagProperties props;

    /** 检索入口。mode 为空则用默认；docId 为空则全库。 */
    public List<ChunkHit> search(String question, String mode, String docId, Integer topK) {
        if (question == null || question.isBlank()) {
            return List.of();
        }
        String m = (mode == null || mode.isBlank()) ? props.getDefaultMode() : mode;
        int k = topK == null || topK <= 0 ? props.getTopK() : topK;
        int pool = Math.max(props.getRerankPool(), k);

        List<ChunkHit> vectorHits = List.of();
        List<ChunkHit> keywordHits = List.of();

        if (MODE_VECTOR.equals(m) || MODE_HYBRID.equals(m)) {
            vectorHits = vectorSearch(question, docId, pool);
        }
        if (MODE_KEYWORD.equals(m) || MODE_HYBRID.equals(m)) {
            keywordHits = keywordSearch(question, docId, pool);
        }

        List<ChunkHit> merged = switch (m) {
            case MODE_VECTOR -> filterByDistance(vectorHits, k);
            case MODE_KEYWORD -> keywordHits.stream().limit(k).toList();
            default -> rrf(vectorHits, keywordHits, k);
        };
        return merged;
    }

    // ---------------- 两路召回 ----------------

    private List<ChunkHit> vectorSearch(String question, String docId, int limit) {
        float[] vec = embedOne(question);
        String literal = VectorTypeHandler.toLiteral(vec);
        return chunkMapper.searchByVector(literal, embedModelRef(), docId, limit);
    }

    private List<ChunkHit> keywordSearch(String question, String docId, int limit) {
        List<String> terms = extractTerms(question);
        if (terms.isEmpty()) {
            return List.of();
        }
        List<ChunkHit> rows = chunkMapper.searchByKeyword(terms, embedModelRef(), docId, limit);
        List<ChunkHit> out = new ArrayList<>();
        for (ChunkHit h : rows) {
            if (h.getHits() != null && h.getHits() > 0) {
                out.add(h);
            }
        }
        return out;
    }

    /** 查询向量也走缓存：同一个问题重复提问时省掉一次模型往返 */
    private float[] embedOne(String text) {
        return embedding.embedOne(text);
    }

    /** chunks.embed_model 列的存法：形如 {@code local:bge-m3}，由 EmbeddingService 统一给出 */
    private String embedModelRef() {
        return embedding.modelColumn();
    }

    // ---------------- 融合 ----------------

    private List<ChunkHit> filterByDistance(List<ChunkHit> hits, int k) {
        List<ChunkHit> out = new ArrayList<>();
        for (ChunkHit h : hits) {
            if (h.getDistance() != null && h.getDistance() <= props.getMaxDistance()) {
                out.add(h);
            }
        }
        out.sort(Comparator.comparingDouble(ChunkHit::getDistance));
        return out.size() > k ? out.subList(0, k) : out;
    }

    /**
     * RRF 融合：score = Σ 1/(k + rank)。
     *
     * <p>关键词模式下距离可能为 null（那条通道不算距离），所以融合后需要把
     * 两边的字段合并到同一条记录上，而不是简单取其中一个。
     */
    private List<ChunkHit> rrf(List<ChunkHit> vectorHits, List<ChunkHit> keywordHits, int k) {
        Map<Long, Double> scores = new LinkedHashMap<>();
        Map<Long, ChunkHit> byId = new LinkedHashMap<>();

        accumulate(vectorHits, scores, byId);
        accumulate(keywordHits, scores, byId);

        // 向量通道的距离门槛在融合后仍然要守住：纯靠排名会把八竿子打不着的段落
        // 也排进来（因为它在那一路里也是"第一名"）
        List<Map.Entry<Long, Double>> ranked = new ArrayList<>(scores.entrySet());
        ranked.sort(Map.Entry.<Long, Double>comparingByValue().reversed());

        List<ChunkHit> out = new ArrayList<>();
        for (Map.Entry<Long, Double> e : ranked) {
            ChunkHit h = byId.get(e.getKey());
            if (h.getDistance() != null && h.getDistance() > props.getMaxDistance()) {
                continue;
            }
            out.add(h);
            if (out.size() >= k) {
                break;
            }
        }
        return out;
    }

    private void accumulate(List<ChunkHit> hits, Map<Long, Double> scores, Map<Long, ChunkHit> byId) {
        for (int i = 0; i < hits.size(); i++) {
            ChunkHit h = hits.get(i);
            scores.merge(h.getId(), 1.0 / (RRF_K + i + 1), Double::sum);
            byId.merge(h.getId(), h, (oldOne, newOne) -> {
                // 保留两路里信息更全的那份：距离与命中数都想要
                if (oldOne.getDistance() == null && newOne.getDistance() != null) {
                    oldOne.setDistance(newOne.getDistance());
                }
                if (oldOne.getHits() == null && newOne.getHits() != null) {
                    oldOne.setHits(newOne.getHits());
                }
                return oldOne;
            });
        }
    }

    // ---------------- 检索词抽取 ----------------

    /**
     * 抽检索词：ASCII 词（长度 ≥2）+ 中文二元组。
     *
     * <p>为什么用二元组而不是分词：PostgreSQL 的 simple 分词器按非字母数字切词，
     * 中文整句没有空格会被当成一个 token，等于不可用；引入外部分词器又增加部署负担。
     * 二元组 + ILIKE 在个人规模下是最简单且真的有效的方案。
     */
    public static List<String> extractTerms(String question) {
        Set<String> terms = new LinkedHashSet<>();

        Matcher words = WORD.matcher(question);
        while (words.find()) {
            terms.add(words.group());
        }

        List<Character> cjk = new ArrayList<>();
        Matcher chars = CJK.matcher(question);
        while (chars.find()) {
            cjk.add(chars.group().charAt(0));
        }
        for (int i = 0; i + 1 < cjk.size(); i++) {
            terms.add("" + cjk.get(i) + cjk.get(i + 1));
        }
        if (cjk.size() == 1) {
            terms.add(String.valueOf(cjk.get(0)));
        }

        List<String> out = new ArrayList<>(terms);
        return out.size() > 24 ? out.subList(0, 24) : out;
    }
}
