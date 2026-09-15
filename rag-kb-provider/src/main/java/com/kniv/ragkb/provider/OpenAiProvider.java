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
 * 任意 OpenAI 兼容端点：DeepSeek / 通义百炼 / Moonshot / 智谱 / 硅基流动 / OpenAI /
 * 本地 vLLM / LM Studio。只要 base 指对、apiKey 填对即可，不用改代码。
 *
 * <p>流式是标准 SSE：每行形如 {@code data: {...}}，以 {@code data: [DONE]} 结束。
 */
public class OpenAiProvider extends AbstractProvider {

    private static final String SSE_PREFIX = "data:";
    private static final String SSE_DONE = "[DONE]";

    public OpenAiProvider(String id, ProviderProperties.Entry cfg, ObjectMapper mapper,
                          Duration timeout, Duration streamIdle) {
        super(id, cfg, mapper, timeout, streamIdle);
    }

    @Override
    public String chat(String model, List<ChatMessage> messages, double temperature) {
        JsonNode resp = postJson("/chat/completions", chatBody(model, messages, temperature, false));
        return resp.path("choices").path(0).path("message").path("content").asText("").strip();
    }

    /**
     * 结构化输出：{@code response_format=json_object}。
     *
     * <p>比 Ollama 那边弱一档 —— 它只保证「是合法 JSON」，不保证字段形状，
     * 形状仍靠提示词里的输出示例。所以这里不传 jsonSchema：OpenAI 兼容端点
     * 普遍不接受 schema（只有 OpenAI 自己的 json_schema 模式接受，且各家方言不一），
     * 传了反而会 400。
     *
     * <p>注意：多数 OpenAI 兼容服务要求提示词里出现过 "json" 字样才允许这个参数 ——
     * 我们的规划/评估提示词都写了「只输出 JSON」，满足。
     */
    @Override
    public String chatJson(String model, List<ChatMessage> messages, double temperature,
                           String jsonSchema) {
        ObjectNode body = chatBody(model, messages, temperature, false);
        body.putObject("response_format").put("type", "json_object");
        JsonNode resp = postJson("/chat/completions", body);
        return resp.path("choices").path(0).path("message").path("content").asText("").strip();
    }

    @Override
    public void chatStream(String model, List<ChatMessage> messages, double temperature,
                           java.util.function.Consumer<String> onToken) {
        postStream("/chat/completions", chatBody(model, messages, temperature, true), line -> {
            String t = line.strip();
            if (t.isEmpty() || !t.startsWith(SSE_PREFIX)) {
                return;   // 保活注释行等
            }
            String payload = t.substring(SSE_PREFIX.length()).strip();
            if (payload.isEmpty() || SSE_DONE.equals(payload)) {
                return;
            }
            try {
                JsonNode n = mapper.readTree(payload);
                if (n.path("error").isObject()) {
                    throw new ProviderException(id,
                            "模型报错：" + n.path("error").path("message").asText(payload));
                }
                String piece = n.path("choices").path(0).path("delta").path("content").asText("");
                if (!piece.isEmpty()) {
                    onToken.accept(piece);
                }
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                // 单行解析失败不中断整段回答
            }
        });
    }

    @Override
    public List<float[]> embed(String model, List<String> texts) {
        ObjectNode body = mapper.createObjectNode();
        body.put("model", model);
        ArrayNode input = body.putArray("input");
        texts.forEach(input::add);

        JsonNode resp = postJson("/embeddings", body);
        JsonNode data = resp.path("data");
        if (!data.isArray() || data.size() != texts.size()) {
            throw new ProviderException(id, "向量条数与请求不一致：期望 " + texts.size()
                    + "，实得 " + data.size());
        }
        List<float[]> out = new ArrayList<>(data.size());
        for (JsonNode item : data) {
            JsonNode vec = item.path("embedding");
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
        JsonNode resp = getJson("/models");
        List<String> names = new ArrayList<>();
        resp.path("data").forEach(m -> {
            String n = m.path("id").asText("");
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
        body.put("temperature", temperature);
        if (stream) {
            body.put("stream", true);
        }
        return body;
    }
}
