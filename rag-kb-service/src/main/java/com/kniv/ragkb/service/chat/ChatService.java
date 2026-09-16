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
import com.kniv.ragkb.service.config.RagProperties;
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
    private final SummaryService summaries;
    private final TurnDocService turnDocs;
    private final RagProperties props;
    /** GPU 空闲门闸：让后台任务避开用户请求 */
    private final com.kniv.ragkb.service.config.GpuGate gpuGate;
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

        // 整个问答过程算「用户在用 GPU」。后台任务（滚动摘要等）会看这个标志，
        // 有用户在跑就等着 —— 本机只有一个推理槽，后台任务跑多久用户就等多久。
        gpuGate.beginUserRequest();
        try {
            return doChat(convId, question, model, retrieval, strategy, docId, onEvent);
        } finally {
            gpuGate.endUserRequest();
        }
    }

    private Outcome doChat(String convId, String question, String model, String retrieval,
                           String strategy, String docId, Consumer<AgentEvent> onEvent) {
        Conversation conv;
        AgenticRagService.HistoryContext hist = AgenticRagService.HistoryContext.EMPTY;
        if (convId == null || convId.isBlank()) {
            conv = create(question, model);
        } else {
            conv = conversations.selectById(convId);
            if (conv == null) {
                throw new IllegalArgumentException("会话不存在：" + convId);
            }
            List<Message> recent = recentMessages(convId);
            // 超出最近窗口的旧轮次：不再是整段丢弃。
            // 分两层取回，可靠性递减 —— 向量召回给细节（会漏），滚动摘要给全局（很粗）
            //
            // 只排除「无论预算怎么裁都会留在提示词里」的那几条，也就是最新的几轮。
            // 原先排除的是整个窗口（16 条），于是被预算裁掉的那些既不在提示词里、
            // 又被召回排除在外，等于直接丢了 —— 而实测第 7 轮裁掉的正是最近 2 轮，
            // 恰恰是「那它呢」这类追问最需要的。
            //
            // 代价：正常情况（没触发裁剪）下召回可能捞到窗口内偏旧的那几条，与提示词
            // 重复。装进提示词前会按内容去一次重，见 AgenticRagService 里的 dedupe。
            int guaranteed = Math.max(2, props.getAgent().getTrimKeepTurns() * 2);
            Set<Long> exclude = new HashSet<>();
            for (int i = Math.max(0, recent.size() - guaranteed); i < recent.size(); i++) {
                if (recent.get(i).getId() != null) {
                    exclude.add(recent.get(i).getId());
                }
            }
            hist = new AgenticRagService.HistoryContext(
                    toChatMessages(recent),
                    historyIndex.retrieve(convId, question, exclude).text(),
                    summaries.summaryOf(convId));
        }

        // 先把 convId 推给前端：否则第一轮回答完才知道会话 id，追问会断链
        if (onEvent != null) {
            onEvent.accept(AgentEvent.of(AgentEvent.META,
                    "convId", conv.getId(), "historyTurns", hist.turns().size() / 2,
                    "hasSummary", hist.summary() != null));
        }

        // 消费掉事件（若调用方只想要最终结果，传 no-op）
        Consumer<AgentEvent> sink = onEvent == null ? e -> { } : onEvent;

        AgenticRagService.AgentResult result =
                "classic".equalsIgnoreCase(strategy)
                        ? rag.classic(question, model, retrieval, docId, hist, sink)
                        : rag.agentic(question, model, retrieval, docId, hist, sink);

        append(conv.getId(), "user", question, null, null, null);
        append(conv.getId(), "assistant", result.answer(),
                toSourcesJson(result.sources()), conv.getProvider(), conv.getModel());
        conversations.touch(conv.getId(), conv.getProvider(), conv.getModel());

        // 两件收尾的事，都在回答之后做、都吞掉自己的异常 ——
        // 它们是锦上添花，不能把一次成功的问答变成失败。
        historyIndex.indexPending(conv.getId());   // 补向量，供后续轮次召回
        summaries.maybeUpdate(conv.getId());       // 攒够一批才真的调模型（异步）
        turnDocs.maybeUpdate(conv.getId());        // 改写窗口外的轮次为自包含笔记（异步）

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
