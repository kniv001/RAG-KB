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
        return body;
    }
}
