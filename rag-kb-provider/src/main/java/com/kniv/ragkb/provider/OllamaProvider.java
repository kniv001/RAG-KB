package com.kniv.ragkb.provider;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.kniv.ragkb.provider.model.ChatMessage;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * 本地 Ollama。
 *
 * <p>接口形态：{@code /api/chat} 与 {@code /api/embed}，流式返回 NDJSON（每行一个完整 JSON）。
 * 与 OpenAI 的差别主要在：参数包在 {@code options} 里、向量接口一次可传多条 input、
 * 流式是逐行 JSON 而不是 SSE。
 */
public class OllamaProvider extends AbstractProvider {

    public OllamaProvider(String id, ProviderProperties.Entry cfg, ObjectMapper mapper,
                          Duration timeout, Duration streamIdle) {
        super(id, cfg, mapper, timeout, streamIdle);
    }

    @Override
    public String chat(String model, List<ChatMessage> messages, double temperature) {
        ObjectNode body = chatBody(model, messages, temperature, false);
        JsonNode resp = postJson("/api/chat", body);
        return resp.path("message").path("content").asText("").strip();
    }

    /**
     * 结构化输出：{@code think:false} + {@code format} 语法约束。
     *
     * <p><b>两个必须同时给，缺一个都白搭</b> —— 这是实测出来的，不是推断：
     * <ul>
     *   <li>只关思考：推理不会消失，它会从 {@code thinking} 通道转进 {@code content} 通道，
     *       于是 「首先，用户的问题是……」 直接把 JSON 冲碎（实测 49.9 秒，输出非法）</li>
     *   <li>只给约束：模型照样先把几千 token 的推理想完，再输出 JSON（实测 49.5 秒）</li>
     *   <li>两个都给：采样器无法输出左花括号之外的第一个字符，模型直接从 JSON 开始写
     *       （实测 0.94 秒，48 个 token）</li>
     * </ul>
     *
     * <p>顺带一提，同在提示词里写 {@code /no_think} 软开关无效 —— Ollama 的 qwen3 模板
     * 不认它，实测思考量纹丝不动（6471 字）。
     */
    @Override
    public String chatJson(String model, List<ChatMessage> messages, double temperature,
                           String jsonSchema) {
        ObjectNode body = chatBody(model, messages, temperature, false);
        body.put("think", false);
        body.set("format", schemaNode(jsonSchema));
        JsonNode resp = postJson("/api/chat", body);
        return resp.path("message").path("content").asText("").strip();
    }

    /** schema 为空时退化为纯 {@code "json"} —— 仍是语法约束，只是形状不设限。 */
    private JsonNode schemaNode(String jsonSchema) {
        if (jsonSchema == null || jsonSchema.isBlank()) {
            return mapper.getNodeFactory().textNode("json");
        }
        try {
            return mapper.readTree(jsonSchema);
        } catch (Exception e) {
            throw new ProviderException(id, "JSON Schema 不是合法 JSON：" + e.getMessage());
        }
    }

    @Override
    public void chatStream(String model, List<ChatMessage> messages, double temperature,
                           java.util.function.Consumer<String> onToken) {
        ObjectNode body = chatBody(model, messages, temperature, true);
        postStream("/api/chat", body, line -> {
            String t = line.strip();
            if (t.isEmpty()) {
                return;
            }
            try {
                JsonNode n = mapper.readTree(t);
                if (n.path("error").isTextual()) {
                    throw new ProviderException(id, "模型报错：" + n.path("error").asText());
                }
                String piece = n.path("message").path("content").asText("");
                if (!piece.isEmpty()) {
                    onToken.accept(piece);
                }
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                // 单行解析失败不应中断整段回答，跳过即可
            }
        });
    }

    @Override
    public List<float[]> embed(String model, List<String> texts) {
        ObjectNode body = mapper.createObjectNode();
        body.put("model", model);
        ArrayNode input = body.putArray("input");
        texts.forEach(input::add);

        JsonNode resp = postJson("/api/embed", body);
        JsonNode embeddings = resp.path("embeddings");
        if (!embeddings.isArray() || embeddings.size() != texts.size()) {
            throw new ProviderException(id, "向量条数与请求不一致：期望 " + texts.size()
                    + "，实得 " + embeddings.size());
        }
        List<float[]> out = new ArrayList<>(embeddings.size());
        for (JsonNode vec : embeddings) {
            float[] v = new float[vec.size()];
            for (int i = 0; i < v.length; i++) {
                v[i] = (float) vec.get(i).asDouble();
            }
            out.add(v);
        }
        return out;
    }

    @Override
    public List<String> listModels() {
        JsonNode resp = getJson("/api/tags");
        List<String> names = new ArrayList<>();
        resp.path("models").forEach(m -> {
            String n = m.path("name").asText("");
            if (!n.isEmpty()) {
                names.add(n);
            }
        });
        return names;
    }

    private ObjectNode chatBody(String model, List<ChatMessage> messages,
                                double temperature, boolean stream) {
        ObjectNode body = mapper.createObjectNode();
        body.put("model", model);
        body.set("messages", messagesArray(messages));
        body.put("stream", stream);
        ObjectNode options = body.putObject("options");
        options.put("temperature", temperature);
        // num_ctx 必须显式传：Ollama 默认 65536，KV 缓存会把显存吃光，
        // 导致向量模型被挤掉、每次交替都重载（实测 4-8 秒 → 传对之后 0.03 秒）
        options.put("num_ctx", cfg == null ? 8192 : cfg.getNumCtx());
        if (cfg != null && cfg.getNumPredict() > 0) {
            options.put("num_predict", cfg.getNumPredict());
        }
        return body;
    }
}
