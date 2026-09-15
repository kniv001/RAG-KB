package com.kniv.ragkb.web;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.provider.ProviderException;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.provider.model.ChatResult;
import com.kniv.ragkb.security.crypto.EncryptedApiFilter;
import com.kniv.ragkb.security.crypto.Envelope;
import com.kniv.ragkb.security.crypto.HybridCryptoService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.constraints.NotBlank;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 模型提供方诊断接口。
 *
 * <p>为什么现在就做：{@code provider} 正式接入 RAG 管线之前，得先能单独验证
 * 「Java 调本地 Ollama 通不通、流式推不推得动」。这些接口就是那个验证面，
 * 之后也长期有用（排查「换个模型怎么就不行了」）。
 *
 * <p>流式用 {@link SseEmitter}：它同时解决两件事 —— 用户体验（不必等整段生成完），
 * 以及 Cloudflare 免费版对源站响应的 100 秒硬超时（先吐出第一个 token 后就不再计时）。
 */
@RestController
@RequestMapping("/api/provider")
@RequiredArgsConstructor
public class ProviderController {

    private final ProviderRegistry registry;
    private final HybridCryptoService crypto;
    private final ObjectMapper objectMapper;
    private final ExecutorService streamPool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "provider-stream");
        t.setDaemon(true);
        return t;
    });

    @GetMapping("/list")
    public R<Map<String, Object>> list() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("providers", registry.describe());
        body.put("defaults", registry.defaults());
        return R.ok(body);
    }

    @GetMapping("/{providerId}/probe")
    public R<Map<String, Object>> probe(@PathVariable String providerId) {
        Map<String, Object> result = registry.probe(providerId);
        return Boolean.TRUE.equals(result.get("ok"))
                ? R.ok(result)
                : R.fail(R.CODE_DEPENDENCY, String.valueOf(result.get("error")), result);
    }

    @Data
    public static class ChatBody {
        /** 形如 local/qwen3:8b；为空则用默认 */
        private String model;
        @NotBlank(message = "消息不能为空")
        private String prompt;
        private String system;
        private Double temperature;
        private Integer maxTokens;
    }

    @PostMapping("/chat")
    public R<Map<String, Object>> chat(@RequestBody ChatBody body) {
        ProviderRegistry.Ref ref = registry.resolveChat(body.getModel());
        double temp = body.getTemperature() == null ? 0.2 : body.getTemperature();

        List<ChatMessage> msgs = new ArrayList<>();
        if (body.getSystem() != null && !body.getSystem().isBlank()) {
            msgs.add(ChatMessage.system(body.getSystem()));
        }
        msgs.add(ChatMessage.user(body.getPrompt()));

        long t0 = System.currentTimeMillis();
        ChatResult result = registry.chat(ref, msgs, temp);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("model", ref.toString());
        out.put("provider", result.providerId());
        out.put("content", result.content());
        out.put("elapsedMs", System.currentTimeMillis() - t0);
        return R.ok(out);
    }

    /**
     * 流式对话。每个事件的 data 都是加密信封 {"iv","d"}（crypto.enabled=false 时才是明文）。
     * 事件名：
     * <pre>
     *   event: meta    data: {"model":"local/qwen3:8b"}
     *   event: token   data: {"t":"片段"}
     *   event: done    data: {"elapsedMs":1234,"chars":56}
     *   event: error   data: {"message":"..."}
     * </pre>
     *
     * <p>为什么逐事件加密而不是交给过滤器统一加密：过滤器会把响应整体缓存下来再处理，
     * 那会彻底破坏流式。所以流式路径由本方法接管加密职责，密钥由过滤器交接过来
     * （见 {@link EncryptedApiFilter#aesKeyOf}）。
     */
    @PostMapping(value = "/chat/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter chatStream(@RequestBody ChatBody body, HttpServletRequest request) {
        // 不设超时：长回答可以推很久，靠 provider 侧的 streamIdleSeconds 兜底
        SseEmitter emitter = new SseEmitter(0L);
        byte[] aesKey = EncryptedApiFilter.aesKeyOf(request);
        ProviderRegistry.Ref ref = registry.resolveChat(body.getModel());
        double temp = body.getTemperature() == null ? 0.2 : body.getTemperature();

        List<ChatMessage> msgs = new ArrayList<>();
        if (body.getSystem() != null && !body.getSystem().isBlank()) {
            msgs.add(ChatMessage.system(body.getSystem()));
        }
        msgs.add(ChatMessage.user(body.getPrompt()));

        streamPool.submit(() -> {
            long t0 = System.currentTimeMillis();
            int[] chars = {0};
            try {
                send(emitter, aesKey, "meta", Map.of("model", ref.toString()));
                registry.chatStream(ref, msgs, temp, piece -> {
                    chars[0] += piece.length();
                    send(emitter, aesKey, "token", Map.of("t", piece));
                });
                send(emitter, aesKey, "done", Map.of(
                        "elapsedMs", System.currentTimeMillis() - t0,
                        "chars", chars[0]));
                emitter.complete();
            } catch (Exception e) {
                try {
                    send(emitter, aesKey, "error", Map.of("message", String.valueOf(e.getMessage())));
                } catch (Exception ignored) {
                    // 客户端已断开，只能放弃
                }
                emitter.completeWithError(e);
            } finally {
                // 密钥是过滤器交接过来的副本，所有权在这里，用完必须清零
                HybridCryptoService.wipe(aesKey);
            }
        });
        return emitter;
    }

    private void send(SseEmitter emitter, byte[] aesKey, String event, Object data) {
        try {
            Object payload = data;
            if (aesKey != null) {
                payload = crypto.encryptBody(aesKey, objectMapper.writeValueAsString(data));
            }
            emitter.send(SseEmitter.event().name(event)
                    .data(payload, MediaType.APPLICATION_JSON));
        } catch (IOException e) {
            throw new ProviderException("sse", "客户端已断开");
        }
    }

    @Data
    public static class EmbedBody {
        private String model;
        @NotBlank(message = "文本不能为空")
        private String text;
    }

    @PostMapping("/embed")
    public R<Map<String, Object>> embed(@RequestBody EmbedBody body) {
        long t0 = System.currentTimeMillis();
        List<float[]> vectors = registry.embed(body.getModel(), List.of(body.getText()));
        float[] v = vectors.get(0);

        // 只回显维度与前若干维，避免把 1024 个数全吐回前端
        List<Float> head = new ArrayList<>();
        for (int i = 0; i < Math.min(6, v.length); i++) {
            head.add(v[i]);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("model", body.getModel());
        out.put("dim", v.length);
        out.put("head", head);
        out.put("elapsedMs", System.currentTimeMillis() - t0);
        return R.ok(out);
    }
}
