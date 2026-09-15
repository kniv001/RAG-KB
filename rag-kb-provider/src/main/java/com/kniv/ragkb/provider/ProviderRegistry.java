package com.kniv.ragkb.provider;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.provider.model.ChatResult;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/**
 * 提供方注册表：把配置装配成实例，并按 {@code providerId/model} 解析调用目标。
 *
 * <p>引用用斜杠分隔（{@code local/qwen3:8b}）而不是冒号 —— 模型名本身常含冒号，
 * 用冒号做分隔符会歧义。
 *
 * <p>上层只跟这里打交道：想做「用户可选本地或云端」，只需把 ref 透传下来。
 */
@Slf4j
@Service
public class ProviderRegistry {

    private final ProviderProperties props;
    private final Map<String, ModelProvider> byId;

    public ProviderRegistry(ProviderProperties props, ObjectMapper mapper) {
        this.props = props;
        Duration timeout = Duration.ofSeconds(props.getTimeoutSeconds());
        Duration streamIdle = Duration.ofSeconds(props.getStreamIdleSeconds());

        Map<String, ModelProvider> map = new LinkedHashMap<>();
        props.getProviders().forEach((id, cfg) -> {
            if (cfg.getBase() == null || cfg.getBase().isBlank()) {
                log.warn("提供方 [{}] 未配置 base，已跳过", id);
                return;
            }
            ModelProvider p = cfg.isOpenAi()
                    ? new OpenAiProvider(id, cfg, mapper, timeout, streamIdle)
                    : new OllamaProvider(id, cfg, mapper, timeout, streamIdle);
            map.put(id, p);
        });
        this.byId = Collections.unmodifiableMap(map);
        log.info("模型提供方已装配 {} 个：{}；默认对话 {}，默认向量 {}",
                byId.size(), byId.keySet(), props.getDefaultChat(), props.getDefaultEmbed());
    }

    /** 解析后的一次调用目标 */
    public record Ref(String providerId, String model) {
        @Override
        public String toString() {
            return providerId + "/" + model;
        }
    }

    /**
     * 解析 {@code provider/model}。
     *
     * @param ref      可为空，空则用 fallback
     * @param fallback 形如 {@code local/qwen3:8b}
     */
    public Ref parse(String ref, String fallback) {
        String s = (ref == null || ref.isBlank()) ? fallback : ref.trim();
        int i = s.indexOf('/');
        if (i <= 0 || i == s.length() - 1) {
            throw new IllegalArgumentException("模型引用格式应为 provider/model，收到：" + s);
        }
        String pid = s.substring(0, i);
        if (!byId.containsKey(pid)) {
            throw new IllegalArgumentException("未配置的提供方：" + pid
                    + "（已装配：" + String.join(", ", byId.keySet()) + "）");
        }
        return new Ref(pid, s.substring(i + 1));
    }

    public Ref resolveChat(String ref) {
        return parse(ref, props.getDefaultChat());
    }

    public Ref resolveEmbed(String ref) {
        return parse(ref, props.getDefaultEmbed());
    }

    public ModelProvider get(String providerId) {
        ModelProvider p = byId.get(providerId);
        if (p == null) {
            throw new IllegalArgumentException("未配置的提供方：" + providerId);
        }
        return p;
    }

    // ---------------- 调用 ----------------

    public ChatResult chat(Ref ref, List<ChatMessage> messages, double temperature) {
        ModelProvider p = get(ref.providerId());
        String content = p.chat(ref.model(), messages, temperature);
        return ChatResult.of(content, ref.providerId(), ref.model());
    }

    /** 只要 JSON 的调用，规划与评估用。见 {@link ModelProvider#chatJson}。 */
    public ChatResult chatJson(Ref ref, List<ChatMessage> messages, double temperature,
                               String jsonSchema) {
        ModelProvider p = get(ref.providerId());
        String content = p.chatJson(ref.model(), messages, temperature, jsonSchema);
        return ChatResult.of(content, ref.providerId(), ref.model());
    }

    public void chatStream(Ref ref, List<ChatMessage> messages, double temperature,
                           Consumer<String> onToken) {
        get(ref.providerId()).chatStream(ref.model(), messages, temperature, onToken);
    }

    /** 流式对话，附带模型的思考片段。见 {@link ModelProvider#chatStream}。 */
    public void chatStream(Ref ref, List<ChatMessage> messages, double temperature,
                           Consumer<String> onToken, Consumer<String> onThinking) {
        get(ref.providerId()).chatStream(ref.model(), messages, temperature, onToken, onThinking);
    }

    public List<float[]> embed(String ref, List<String> texts) {
        Ref r = resolveEmbed(ref);
        return get(r.providerId()).embed(r.model(), texts);
    }

    // ---------------- 探测与描述 ----------------

    /** 探测某提供方是否可用，并列出它当前真实可用的模型。 */
    public Map<String, Object> probe(String providerId) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("provider", providerId);
        ModelProvider p = byId.get(providerId);
        if (p == null) {
            out.put("ok", false);
            out.put("error", "未配置的提供方");
            return out;
        }
        out.put("kind", p.kind());
        try {
            List<String> models = p.listModels();
            out.put("ok", true);
            out.put("models", models);
            out.put("count", models.size());
        } catch (Exception e) {
            out.put("ok", false);
            out.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        return out;
    }

    /** 供前端渲染选择器。API Key 只回传「是否已设置」。 */
    public List<Map<String, Object>> describe() {
        List<Map<String, Object>> list = new ArrayList<>();
        props.getProviders().forEach((id, cfg) -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", id);
            m.put("kind", cfg.getKind());
            m.put("label", cfg.displayLabel());
            m.put("base", cfg.getBase());
            m.put("chatModels", cfg.getChatModels());
            m.put("embedModels", cfg.getEmbedModels());
            m.put("hasKey", cfg.getApiKey() != null && !cfg.getApiKey().isBlank());
            m.put("assembled", byId.containsKey(id));
            list.add(m);
        });
        return list;
    }

    public Map<String, Object> defaults() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chat", props.getDefaultChat());
        m.put("embed", props.getDefaultEmbed());
        return m;
    }
}
