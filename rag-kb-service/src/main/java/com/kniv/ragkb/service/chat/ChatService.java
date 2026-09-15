package com.kniv.ragkb.service.chat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.ConversationMapper;
import com.kniv.ragkb.dao.mapper.MessageMapper;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.domain.entity.Conversation;
import com.kniv.ragkb.domain.entity.Message;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.AgentEvent;
import com.kniv.ragkb.service.agent.AgenticRagService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.function.Consumer;

/**
 * 会话持久化与多轮问答。
 *
 * <p>会话与消息存 PostgreSQL。每条助手消息把当轮召回的来源一并存下 ——
 * 没有来源记录的 RAG 回答事后无法验证可信度，等于把「可追溯」这个核心价值丢了。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChatService {

    /** 给模型看的历史轮数上限。太长会挤占资料与问题的上下文预算 */
    private static final int HISTORY_LIMIT = 16;

    private final ConversationMapper conversations;
    private final MessageMapper messages;
    private final AgenticRagService rag;
    private final HistoryIndexService historyIndex;
    private final ObjectMapper mapper;

    public record Outcome(String convId, String answer, List<ChunkHit> sources,
                          int rounds, List<String> queries) {
    }

    /**
     * 一轮问答：解析/新建会话 → 跑 agent → 落库。
     *
     * @param onEvent 流式回调；会在最前面收到一个带 convId 的 meta 事件，
     *                这样前端在回答生成完之前就拿到会话 id，可以接着追问
     */
    public Outcome chat(String convId, String question, String model, String retrieval,
                        String strategy, String docId, Consumer<AgentEvent> onEvent) {
        if (question == null || question.isBlank()) {
            throw new IllegalArgumentException("问题为空");
        }

        Conversation conv;
        List<ChatMessage> history;
        String excerpt = null;
        if (convId == null || convId.isBlank()) {
            conv = create(question, model);
            history = List.of();
        } else {
            conv = conversations.selectById(convId);
            if (conv == null) {
                throw new IllegalArgumentException("会话不存在：" + convId);
            }
            List<Message> recent = recentMessages(convId);
            history = toChatMessages(recent);
            // 超出最近窗口的旧轮次：不再是整段丢弃，而是向量召回。
            // 排除窗口内那些 —— 它们已经在提示词里了，再召回一遍是纯浪费。
            excerpt = historyIndex.retrieve(convId, question, idsOf(recent)).text();
        }

        // 先把 convId 推给前端：否则第一轮回答完才知道会话 id，追问会断链
        if (onEvent != null) {
            onEvent.accept(AgentEvent.of(AgentEvent.META,
                    "convId", conv.getId(), "historyTurns", history.size() / 2));
        }

        // 消费掉事件（若调用方只想要最终结果，传 no-op）
        Consumer<AgentEvent> sink = onEvent == null ? e -> { } : onEvent;

        AgenticRagService.AgentResult result =
                "classic".equalsIgnoreCase(strategy)
                        ? rag.classic(question, model, retrieval, docId, history, excerpt, sink)
                        : rag.agentic(question, model, retrieval, docId, history, excerpt, sink);

        append(conv.getId(), "user", question, null, null, null);
        append(conv.getId(), "assistant", result.answer(),
                toSourcesJson(result.sources()), conv.getProvider(), conv.getModel());
        conversations.touch(conv.getId(), conv.getProvider(), conv.getModel());

        // 把刚写入的两条补上向量，供后续轮次召回。放在回答之后，用户已经拿到结果了；
        // 内部吞掉异常 —— 索引只是锦上添花，不能让它把一次成功的问答变成失败
        historyIndex.indexPending(conv.getId());

        return new Outcome(conv.getId(), result.answer(), result.sources(),
                result.rounds(), result.queries());
    }

    // ---------------- 会话 ----------------

    public Conversation create(String seedTitle, String model) {
        Conversation c = new Conversation();
        c.setId(UUID.randomUUID().toString().replace("-", "").substring(0, 12));
        String title = seedTitle == null ? "新对话" : seedTitle.strip();
        c.setTitle(title.length() > 30 ? title.substring(0, 30) : title);
        if (model != null && model.contains("/")) {
            int i = model.indexOf('/');
            c.setProvider(model.substring(0, i));
            c.setModel(model.substring(i + 1));
        }
        conversations.insert(c);
        return c;
    }

    public List<Conversation> list(int limit) {
        return conversations.listWithTurns(limit);
    }

    public List<Message> messages(String convId) {
        return messages.listByConversation(convId);
    }

    public boolean remove(String convId) {
        return conversations.deleteById(convId) > 0;
    }

    /** 供模型看的历史（时间正序，只含 role/content）。 */
    public List<ChatMessage> recentHistory(String convId) {
        return toChatMessages(recentMessages(convId));
    }

    /** 最近窗口的原始消息（时间正序）。带 id，供历史召回排除用。 */
    private List<Message> recentMessages(String convId) {
        List<Message> recent = messages.recentForContext(convId, HISTORY_LIMIT);
        Collections.reverse(recent);   // 查询是倒序取的
        return recent;
    }

    private List<ChatMessage> toChatMessages(List<Message> recent) {
        List<ChatMessage> out = new ArrayList<>(recent.size());
        for (Message m : recent) {
            if (m.getContent() != null && !m.getContent().isBlank()) {
                out.add(new ChatMessage(m.getRole(), m.getContent()));
            }
        }
        return out;
    }

    private Set<Long> idsOf(List<Message> msgs) {
        Set<Long> ids = new HashSet<>(msgs.size() * 2);
        for (Message m : msgs) {
            if (m.getId() != null) {
                ids.add(m.getId());
            }
        }
        return ids;
    }

    private void append(String convId, String role, String content,
                        String sourcesJson, String provider, String model) {
        Message m = new Message();
        m.setConvId(convId);
        m.setRole(role);
        m.setContent(content == null ? "" : content);
        m.setSources(sourcesJson);
        m.setProvider(provider);
        m.setModel(model);
        messages.insert(m);
    }

    private String toSourcesJson(List<ChunkHit> hits) {
        List<Map<String, Object>> list = new ArrayList<>();
        for (ChunkHit h : hits) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("docId", h.getDocId());
            m.put("docName", h.getDocName());
            m.put("seq", h.getSeq());
            m.put("score", h.getScore());
            m.put("distance", h.getDistance());
            list.add(m);
        }
        try {
            return mapper.writeValueAsString(list);
        } catch (Exception e) {
            return "[]";
        }
    }
}
