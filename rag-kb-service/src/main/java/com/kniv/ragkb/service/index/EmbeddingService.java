package com.kniv.ragkb.service.index;

import com.kniv.ragkb.provider.ProviderProperties;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.service.cache.CacheService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.BiConsumer;

/**
 * 带缓存的向量化。
 *
 * <p>缓存命中时重建索引几乎瞬间完成 —— 向量化是整个索引链最慢的一步。
 * 查缓存 → 只对未命中的部分调模型 → 结果写回。
 *
 * <p>把这一层单独抽出来而不是塞进 Retriever：检索侧也要用它，
 * 否则同一个问题重复提问每次都白算一次查询向量。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class EmbeddingService {

    private final ProviderRegistry providers;
    private final ProviderProperties providerProps;
    private final CacheService cache;

    /** 当前向量模型的外部引用（形如 local/bge-m3） */
    public String modelRef() {
        return providerProps.getDefaultEmbed();
    }

    /** 落库用的标识（形如 local:bge-m3）—— 与 Python 版一致，两套系统共用一个库 */
    public String modelColumn() {
        return providerProps.getDefaultEmbed().replace('/', ':');
    }

    /**
     * @param onProgress 可选回调 (已完成数, 总数)，用于索引进度上报
     */
    public List<float[]> embed(List<String> texts, BiConsumer<Integer, Integer> onProgress) {
        if (texts.isEmpty()) {
            return List.of();
        }
        String model = modelColumn();
        CacheService.EmbeddingLookup lookup = cache.lookupEmbeddings(texts, model);

        List<float[]> out = new ArrayList<>(texts.size());
        for (int i = 0; i < texts.size(); i++) {
            out.add(null);
        }
        lookup.hits().forEach(out::set);
        if (onProgress != null && !lookup.hits().isEmpty()) {
            onProgress.accept(lookup.hits().size(), texts.size());
        }

        if (!lookup.misses().isEmpty()) {
            List<String> missTexts = new ArrayList<>(lookup.misses().size());
            for (int idx : lookup.misses()) {
                missTexts.add(texts.get(idx));
            }
            List<float[]> vectors = providers.embed(modelRef(), missTexts);
            if (vectors.size() != missTexts.size()) {
                throw new IllegalStateException("向量条数与请求不一致：期望 "
                        + missTexts.size() + "，实得 " + vectors.size());
            }
            cache.storeEmbeddings(missTexts, vectors, model);

            for (int k = 0; k < lookup.misses().size(); k++) {
                out.set(lookup.misses().get(k), vectors.get(k));
            }
            if (onProgress != null) {
                onProgress.accept(out.size(), texts.size());
            }
        }
        return out;
    }

    public float[] embedOne(String text) {
        List<float[]> v = embed(List.of(text), null);
        if (v.isEmpty() || v.get(0) == null || v.get(0).length == 0) {
            throw new IllegalStateException("向量化返回为空，检查 embedding 提供方是否可用");
        }
        return v.get(0);
    }

    /** 批量分批（小模型一次吃太多会超时） */
    public List<float[]> embedBatched(List<String> texts, int batchSize,
                                      BiConsumer<Integer, Integer> onProgress) {
        List<float[]> out = new ArrayList<>(texts.size());
        for (int i = 0; i < texts.size(); i += batchSize) {
            final int offset = i;                 // lambda 只能捕获 effectively final 的变量
            int end = Math.min(i + batchSize, texts.size());
            List<float[]> part = embed(texts.subList(i, end),
                    (done, total) -> {
                        if (onProgress != null) {
                            onProgress.accept(offset + done, texts.size());
                        }
                    });
            out.addAll(part);
        }
        return out;
    }

    /** 给健康检查用 */
    public Map<String, Object> check() {
        try {
            float[] v = embedOne("维度自检");
            return Map.of("ok", true, "model", modelRef(), "dim", v.length);
        } catch (Exception e) {
            return Map.of("ok", false, "model", modelRef(),
                    "error", e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }
}
