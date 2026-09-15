package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.security.crypto.EncryptedApiFilter;
import com.kniv.ragkb.security.crypto.HybridCryptoService;
import com.kniv.ragkb.service.agent.AgentEvent;
import com.kniv.ragkb.service.agent.AgenticRagService;
import com.kniv.ragkb.service.config.RagProperties;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.constraints.NotBlank;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 问答接口。
 *
 * <p>两种策略：
 * <ul>
 *   <li>{@code agent} —— 规划 → 检索 → 评估 →（不足则换查询再来）→ 生成</li>
 *   <li>{@code classic} —— 单轮检索即生成，作为对照与降级路径</li>
 * </ul>
 * 两者共用同一套检索与生成提示词，差别只在「要不要多轮决策」。
 *
 * <p>流式事件的类型见 {@link AgentEvent}：meta / plan / retrieve / assess / answer / done / error。
 */
@RestController
@RequestMapping("/api/ask")
@RequiredArgsConstructor
public class AskController {

    private final AgenticRagService rag;
    private final RagProperties ragProps;
    private final SseSupport sse;
    private final ExecutorService askPool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "ask-stream");
        t.setDaemon(true);
        return t;
    });

    @Data
    public static class AskBody {
        @NotBlank(message = "问题不能为空")
        private String question;

        /** 形如 local/qwen3:8b；为空用默认 */
        private String model;

        /** 检索模式 vector / keyword / hybrid；为空用默认 */
        private String retrieval;

        /** agent / classic；为空按配置默认 */
        private String strategy;

        /** 限定在某篇文档内检索 */
        private String docId;

        private Integer topK;
    }

    @PostMapping(value = "/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter askStream(@RequestBody AskBody body, HttpServletRequest request) {
        SseEmitter emitter = sse.open();
        byte[] aesKey = EncryptedApiFilter.aesKeyOf(request);

        askPool.submit(() -> {
            long t0 = System.currentTimeMillis();
            try {
                sse.send(emitter, aesKey, AgentEvent.META,
                        Map.of("question", body.getQuestion(),
                                "strategy", strategyOf(body)));
                AgenticRagService.AgentResult result = run(body,
                        event -> sse.send(emitter, aesKey, event.type(), event.data()));
                sse.send(emitter, aesKey, AgentEvent.DONE, Map.of(
                        "rounds", result.rounds(),
                        "contexts", result.sources().size(),
                        "queries", result.queries(),
                        "elapsedMs", System.currentTimeMillis() - t0));
                emitter.complete();
            } catch (Exception e) {
                sse.sendErrorQuietly(emitter, aesKey, e);
                emitter.completeWithError(e);
            } finally {
                // 密钥是过滤器交接过来的副本，所有权在这里，必须清零
                HybridCryptoService.wipe(aesKey);
            }
        });
        return emitter;
    }

    @PostMapping
    public R<Map<String, Object>> ask(@RequestBody AskBody body) {
        long t0 = System.currentTimeMillis();
        AgenticRagService.AgentResult result = run(body, event -> { });

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("answer", result.answer());
        out.put("rounds", result.rounds());
        out.put("queries", result.queries());
        out.put("sources", describe(result.sources()));
        out.put("elapsedMs", System.currentTimeMillis() - t0);
        return R.ok(out);
    }

    // ---------------- 内部 ----------------

    private String strategyOf(AskBody body) {
        if (body.getStrategy() != null && !body.getStrategy().isBlank()) {
            return body.getStrategy();
        }
        return ragProps.getAgent().isEnabled() ? "agent" : "classic";
    }

    private AgenticRagService.AgentResult run(AskBody body, java.util.function.Consumer<AgentEvent> onEvent) {
        if ("classic".equalsIgnoreCase(strategyOf(body))) {
            return rag.classic(body.getQuestion(), body.getModel(),
                    body.getRetrieval(), body.getDocId(), onEvent);
        }
        return rag.agentic(body.getQuestion(), body.getModel(),
                body.getRetrieval(), body.getDocId(), onEvent);
    }

    private List<Map<String, Object>> describe(List<ChunkHit> hits) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (ChunkHit h : hits) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("docId", h.getDocId());
            m.put("docName", h.getDocName());
            m.put("seq", h.getSeq());
            m.put("distance", h.getDistance());
            m.put("score", h.getScore());
            m.put("hits", h.getHits());
            m.put("preview", h.getContent() == null ? "" :
                    h.getContent().substring(0, Math.min(200, h.getContent().length())));
            out.add(m);
        }
        return out;
    }
}
