package com.kniv.ragkb.service.cache;

import com.kniv.ragkb.dao.mapper.CacheMapper;
import com.kniv.ragkb.domain.dto.CachedAnswer;
import com.kniv.ragkb.domain.dto.CachedVector;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 三层缓存。全部落在 PostgreSQL，重启不丢。
 *
 * <table>
 *   <tr><th>层</th><th>键</th><th>省掉什么</th></tr>
 *   <tr><td>向量</td><td>sha256(文本 + 模型)</td><td>最贵的向量计算</td></tr>
 *   <tr><td>解析</td><td>sha256(文件内容)</td><td>PDF / DOCX 解析</td></tr>
 *   <tr><td>回答</td><td>sha256(问题 + 上下文哈希 + 历史哈希 + provider + model + 温度)</td><td>本地模型数十秒的推理</td></tr>
 * </table>
 *
 * <p>键里塞进所有影响结果的参数，是为了「改了输入就自动不命中」——
 * 于是永远不存在返回陈旧结果的窗口，清空缓存也就只是释放空间。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class CacheService {

    private static final String SEP = "\u0000";

    private final CacheMapper mapper;

    // ---------------- 键 ----------------

    public static String hash(String... parts) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < parts.length; i++) {
                if (i > 0) {
                    sb.append(SEP);
                }
                sb.append(parts[i] == null ? "" : parts[i]);
            }
            byte[] d = md.digest(sb.toString().getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(d.length * 2);
            for (byte b : d) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("JVM 不支持 SHA-256", e);
        }
    }

    public static String embeddingKey(String text, String model) {
        return hash(text, model);
    }

    public static String contentKey(String content) {
        return hash(content);
    }

    /** 上下文哈希：资料变了答案就该重算，这是回答缓存不返回陈旧内容的关键。 */
    public static String contextHash(List<String> chunkContents) {
        return hash(chunkContents.toArray(new String[0]));
    }

    /** 历史哈希：多轮对话里，历史变了答案也会变，必须进键。 */
    public static String historyHash(List<String> historyLines) {
        return historyLines == null || historyLines.isEmpty()
                ? "" : hash(historyLines.toArray(new String[0]));
    }

    /**
     * 回答缓存键。
     *
     * <p><b>{@code promptHash} 必须进键</b>：原来只算 question / 资料 / 历史 / 模型 / 温度，
     * **系统提示不在里面** —— 于是改提示词之后，同一批问题照样命中**用旧提示词跑出来的**答案。
     * 后果有两层：
     * <ol>
     *   <li>生产上：改了提示词，老问题的行为不会变</li>
     *   <li>实验上：**任何提示词 A/B 都做不了** —— 第二臂全部命中第一臂的结果</li>
     * </ol>
     * 这与同一天踩到的「换解析器不会让解析缓存失效」（`cache_parses`）是同一类坑。
     *
     * <p>这里**不写手动版本号，而是算实际提示词的哈希** —— 手动的要靠人记得加，
     * 而"记得加"正是这类坑的成因；哈希则是改了就自动失效，包括开关切换（如思考形状）。
     */
    public static String answerKey(String question, String ctxHash, String histHash,
                                   String provider, String model, double temperature,
                                   String promptHash) {
        return hash(question, ctxHash, histHash, provider, model, String.valueOf(temperature),
                promptHash);
    }

    // ---------------- 向量 ----------------

    /** 批量查。返回 {下标 → 向量} 与未命中的下标列表，一次查库避免逐条往返。 */
    public record EmbeddingLookup(Map<Integer, float[]> hits, List<Integer> misses) {
    }

    public EmbeddingLookup lookupEmbeddings(List<String> texts, String model) {
        Map<Integer, float[]> hits = new HashMap<>();
        List<Integer> misses = new ArrayList<>();
        if (texts.isEmpty()) {
            return new EmbeddingLookup(hits, misses);
        }

        List<String> keys = new ArrayList<>(texts.size());
        for (String t : texts) {
            keys.add(embeddingKey(t, model));
        }

        Map<String, String> found = new HashMap<>();
        try {
            List<CachedVector> rows = mapper.findEmbeddings(keys);
            for (CachedVector row : rows) {
                found.put(row.getKey(), row.getVec());
            }
            if (!found.isEmpty()) {
                mapper.touchEmbeddings(new ArrayList<>(found.keySet()));
            }
        } catch (Exception e) {
            // 缓存故障不能让检索挂掉：降级为全部未命中
            log.warn("向量缓存查询失败，按未命中处理：{}", e.getMessage());
        }

        for (int i = 0; i < keys.size(); i++) {
            String vec = found.get(keys.get(i));
            if (vec == null) {
                misses.add(i);
            } else {
                hits.put(i, VectorTypeHandler.parse(vec));
            }
        }
        return new EmbeddingLookup(hits, misses);
    }

    public void storeEmbeddings(List<String> texts, List<float[]> vectors, String model) {
        if (texts.isEmpty()) {
            return;
        }
        List<Map<String, Object>> rows = new ArrayList<>(texts.size());
        for (int i = 0; i < texts.size(); i++) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("key", embeddingKey(texts.get(i), model));
            row.put("model", model);
            row.put("vec", VectorTypeHandler.toLiteral(vectors.get(i)));
            row.put("dim", vectors.get(i).length);
            rows.add(row);
        }
        try {
            mapper.insertEmbeddings(rows);
        } catch (Exception e) {
            log.warn("向量缓存写入失败（不影响本次结果）：{}", e.getMessage());
        }
    }

    // ---------------- 解析 ----------------

    public String getParse(String key) {
        try {
            String text = mapper.findParse(key);
            if (text != null) {
                mapper.touchParse(key);
            }
            return text;
        } catch (Exception e) {
            log.warn("解析缓存查询失败：{}", e.getMessage());
            return null;
        }
    }

    public void putParse(String key, String name, String text) {
        try {
            mapper.insertParse(key, name, text, text.length());
        } catch (Exception e) {
            log.warn("解析缓存写入失败：{}", e.getMessage());
        }
    }

    // ---------------- 回答 ----------------

    public CachedAnswer getAnswer(String key) {
        try {
            CachedAnswer a = mapper.findAnswer(key);
            if (a != null) {
                mapper.touchAnswer(key);
            }
            return a;
        } catch (Exception e) {
            log.warn("回答缓存查询失败：{}", e.getMessage());
            return null;
        }
    }

    public void putAnswer(String key, String question, String answer,
                          String provider, String model, String sourcesJson) {
        try {
            mapper.insertAnswer(key, question, answer, provider, model,
                    sourcesJson == null ? "[]" : sourcesJson);
        } catch (Exception e) {
            log.warn("回答缓存写入失败：{}", e.getMessage());
        }
    }

    // ---------------- 运维 ----------------

    public Map<String, Object> stats() {
        Map<String, Object> out = new LinkedHashMap<>();
        try {
            for (Map<String, Object> row : mapper.stats()) {
                out.put(String.valueOf(row.get("kind")), row);
            }
        } catch (Exception e) {
            out.put("error", e.getMessage());
        }
        return out;
    }

    /** which 为空则全清。返回各层删除的行数。 */
    public Map<String, Integer> clear(String which) {
        Map<String, Integer> removed = new LinkedHashMap<>();
        if (which == null || which.isBlank() || "embeddings".equals(which)) {
            removed.put("embeddings", mapper.clearEmbeddings());
        }
        if (which == null || which.isBlank() || "answers".equals(which)) {
            removed.put("answers", mapper.clearAnswers());
        }
        if (which == null || which.isBlank() || "parses".equals(which)) {
            removed.put("parses", mapper.clearParses());
        }
        return removed;
    }
}
