package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.domain.entity.Conversation;
import com.kniv.ragkb.domain.entity.Message;
import com.kniv.ragkb.security.crypto.EncryptedApiFilter;
import com.kniv.ragkb.security.crypto.HybridCryptoService;
import com.kniv.ragkb.service.agent.AgentEvent;
import com.kniv.ragkb.service.chat.ChatService;
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
 * 多轮对话。
 *
 * <p>路径以 {@code /stream} 结尾是刻意的 —— 加密过滤器按 `/**‍/stream` 识别流式端点，
 * 那类端点只解密请求、响应由这里逐事件加密（过滤器整体缓存响应的做法会破坏流式）。
 */
@RestController
@RequestMapping("/api/chat")
@RequiredArgsConstructor
public class ChatController {

    private final ChatService chatService;
    private final SseSupport sse;
    private final ExecutorService chatPool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "chat-stream");
        t.setDaemon(true);
        return t;
    });

    @Data
    public static class ChatBody {
        @NotBlank(message = "问题不能为空")
        private String question;

        /** 为空则新建会话 */
        private String convId;

        private String model;
        private String retrieval;
        private String strategy;
        private String docId;
    }

    @PostMapping(value = "/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter stream(@RequestBody ChatBody body, HttpServletRequest request) {
        SseEmitter emitter = sse.open();
        byte[] aesKey = EncryptedApiFilter.aesKeyOf(request);

        chatPool.submit(() -> {
            try {
                ChatService.Outcome o = chatService.chat(body.getConvId(), body.getQuestion(),
                        body.getModel(), body.getRetrieval(), body.getStrategy(), body.getDocId(),
                        event -> sse.send(emitter, aesKey, event.type(), event.data()));
                // 收尾事件必须发：来源（sources）只在这里能拿到，漏掉的话
                // 流式回答下面永远不显示引用出处，而这正是 RAG 的可信度所在
                sse.send(emitter, aesKey, AgentEvent.DONE, Map.of(
                        "convId", o.convId(),
                        "rounds", o.rounds(),
                        "queries", o.queries(),
                        "sources", describe(o.sources())));
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
    public R<Map<String, Object>> chat(@RequestBody ChatBody body) {
        ChatService.Outcome o = chatService.chat(body.getConvId(), body.getQuestion(),
                body.getModel(), body.getRetrieval(), body.getStrategy(), body.getDocId(), null);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("convId", o.convId());
        out.put("answer", o.answer());
        out.put("rounds", o.rounds());
        out.put("queries", o.queries());
        out.put("sources", describe(o.sources()));
        return R.ok(out);
    }

    @GetMapping("/conversations")
    public R<Map<String, Object>> conversations(@RequestParam(defaultValue = "50") int limit) {
        List<Conversation> list = chatService.list(Math.max(1, Math.min(limit, 200)));
        List<Map<String, Object>> rows = new ArrayList<>(list.size());
        for (Conversation c : list) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", c.getId());
            m.put("title", c.getTitle());
            m.put("provider", c.getProvider());
            m.put("model", c.getModel());
            m.put("updatedAt", c.getUpdatedAt() == null ? null : c.getUpdatedAt().toString());
            rows.add(m);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("count", rows.size());
        out.put("conversations", rows);
        return R.ok(out);
    }

    @GetMapping("/conversations/{convId}")
    public R<Map<String, Object>> conversation(@PathVariable String convId) {
        Conversation conv = chatService.list(200).stream()
                .filter(c -> c.getId().equals(convId)).findFirst().orElse(null);
        if (conv == null) {
            return R.fail(R.CODE_NOT_FOUND, "会话不存在");
        }
        List<Map<String, Object>> msgs = new ArrayList<>();
        for (Message m : chatService.messages(convId)) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id", m.getId());
            row.put("role", m.getRole());
            row.put("content", m.getContent());
            row.put("provider", m.getProvider());
            row.put("model", m.getModel());
            row.put("createdAt", m.getCreatedAt() == null ? null : m.getCreatedAt().toString());
            if (m.getSources() != null && !m.getSources().isBlank()) {
                try {
                    row.put("sources", new com.fasterxml.jackson.databind.ObjectMapper()
                            .readTree(m.getSources()));
                } catch (Exception ignored) {
                    // 来源解析失败不影响消息本身
                }
            }
            msgs.add(row);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("conversation", Map.of("id", conv.getId(), "title", conv.getTitle()));
        out.put("messages", msgs);
        return R.ok(out);
    }

    @DeleteMapping("/conversations/{convId}")
    public R<Map<String, Object>> delete(@PathVariable String convId) {
        return chatService.remove(convId)
                ? R.ok(Map.of("removed", convId))
                : R.fail(R.CODE_NOT_FOUND, "会话不存在");
    }

    private List<Map<String, Object>> describe(List<ChunkHit> hits) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (ChunkHit h : hits) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("docId", h.getDocId());
            m.put("docName", h.getDocName());
            m.put("seq", h.getSeq());
            m.put("score", h.getScore());
            m.put("distance", h.getDistance());
            m.put("hits", h.getHits());
            m.put("preview", h.getContent() == null ? "" :
                    h.getContent().substring(0, Math.min(200, h.getContent().length())));
            out.add(m);
        }
        return out;
    }
}
