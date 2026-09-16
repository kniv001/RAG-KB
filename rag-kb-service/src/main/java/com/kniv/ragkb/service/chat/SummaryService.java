package com.kniv.ragkb.service.chat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.ConversationMapper;
import com.kniv.ragkb.dao.mapper.MessageMapper;
import com.kniv.ragkb.domain.entity.Conversation;
import com.kniv.ragkb.domain.entity.Message;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.JsonExtract;
import com.kniv.ragkb.service.config.RagProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 会话滚动摘要 —— 历史索引漏召时的兜底。
 *
 * <p><b>为什么需要</b>：向量检索只给 top-k，召不回就丢了，而且<b>没有第二次机会</b>。
 * 指代与省略尤其致命 —— 用户问「那它呢」，检索很可能匹配不上定义「它」的那一轮，
 * 因为那轮的文本里根本没有「它」这个词。摘要覆盖全部历史，粗糙但不会完全丢。
 *
 * <p><b>成本怎么压下来的</b>：摘要本该是一次完整的生成调用（在这个模型上约 20 秒），
 * 那样后台跑也会和新请求抢 GPU。这里走结构化输出（关思考 + 语法约束），
 * 实测同类调用 0.3~1.6 秒 —— 摘要不需要深度推理，它是归纳不是解题。
 *
 * <p><b>为什么是增量的</b>：每次都重读整个会话重算的话，成本随轮数线性增长。
 * 这里只把「新掉出最近窗口」的那几条并入已有摘要，锚点是
 * {@code conversations.summary_upto}。
 *
 * <p><b>为什么异步</b>：它只是锦上添花，不该让用户多等一秒。
 * 用显式线程池而不是 {@code @Async} —— 后者靠代理生效，同类内部调用会静默失效。
 * 单线程是为了最多只有一个摘要在算，避免长会话里堆积。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class SummaryService {

    private static final String PROMPT = """
            你是对话摘要器。把「新增对话」并入「已有摘要」，输出一段连贯的中文摘要。

            要求：
            1. 保留具体信息：讨论的主题、得出的结论、用户明确表达过的偏好或约束、尚未解决的问题。
            2. 用户明确要求记住的任何内容必须原样保留 —— 名字、代号、数字、约定、日期。
               这类信息一旦丢掉就再也找不回来，比主题概括重要得多。
            3. 不要写成「用户问了…助手回答了…」的流水账，直接写内容本身。
            4. 已有摘要里仍然重要的信息要保留，只有被推翻或过时的才丢掉。
            5. 全文不超过 300 字。

            只输出 JSON：{"summary":"摘要正文"}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"]}""";

    private final MessageMapper messages;
    private final ConversationMapper conversations;
    private final ProviderRegistry providers;
    private final RagProperties props;
    private final ObjectMapper mapper;

    private final ExecutorService pool = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "conv-summary");
        t.setDaemon(true);
        return t;
    });

    /** 同一会话同一时刻只算一次，避免连续追问时堆积 */
    private final Set<String> inFlight = ConcurrentHashMap.newKeySet();

    /** 读摘要给提示词用。没有就返回 null。 */
    public String summaryOf(String convId) {
        if (convId == null || convId.isBlank()) {
            return null;
        }
        Conversation c = conversations.selectById(convId);
        String s = c == null ? null : c.getSummary();
        return (s == null || s.isBlank()) ? null : s;
    }

    /**
     * 触发一次异步增量更新。不满足条件就什么都不做 —— 这个方法会在每轮问答后调用，
     * 必须廉价。
     */
    public void maybeUpdate(String convId) {
        RagProperties.Summary cfg = props.getSummary();
        if (!cfg.isEnabled() || convId == null || convId.isBlank()) {
            return;
        }
        try {
            // 「最近窗口之外最新的那一条」—— 窗口内的原文还要给模型看，
            // 提前摘进摘要里是重复的
            Long upto = messages.idAtOffset(convId, cfg.getWindowMessages());
            if (upto == null) {
                return;   // 消息还没多到溢出窗口
            }
            Conversation conv = conversations.selectById(convId);
            if (conv == null) {
                return;
            }
            long from = conv.getSummaryUpto() == null ? 0L : conv.getSummaryUpto();
            if (upto <= from) {
                return;   // 没有新东西掉出窗口
            }
            List<Message> fresh = messages.listBetween(convId, from, upto, cfg.getBatchMessages());
            if (fresh.size() < cfg.getBatchMessages()) {
                return;   // 攒够一批再算，避免每轮都调一次模型
            }
            if (!inFlight.add(convId)) {
                return;   // 上一次还没算完
            }
            String old = conv.getSummary();
            long newUpto = fresh.get(fresh.size() - 1).getId();
            pool.submit(() -> {
                try {
                    String merged = summarize(old, fresh);
                    if (merged != null && !merged.isBlank()) {
                        conversations.updateSummary(convId, merged, newUpto);
                        log.debug("会话 {} 摘要已更新到消息 {}（{} 字）", convId, newUpto, merged.length());
                    }
                } catch (Exception e) {
                    log.warn("摘要更新失败（不影响问答）：{}", e.getMessage());
                } finally {
                    inFlight.remove(convId);
                }
            });
        } catch (Exception e) {
            log.warn("摘要调度失败（不影响问答）：{}", e.getMessage());
        }
    }

    private String summarize(String old, List<Message> fresh) throws Exception {
        StringBuilder user = new StringBuilder();
        user.append("已有摘要：\n").append(old == null || old.isBlank() ? "（无，这是第一次）" : old)
                .append("\n\n新增对话：\n");
        for (Message m : fresh) {
            String text = m.getContent() == null ? "" : m.getContent().replace('\n', ' ').strip();
            if (text.length() > 400) {
                text = text.substring(0, 400) + "…";
            }
            user.append("assistant".equals(m.getRole()) ? "助手：" : "用户：").append(text).append('\n');
        }

        ProviderRegistry.Ref ref = providers.resolveChat(props.getAgent().getUtilityModel());
        String reply = providers.chatJson(ref,
                List.of(ChatMessage.system(PROMPT), ChatMessage.user(user.toString())),
                0.2, SCHEMA).content();

        JsonNode node = JsonExtract.parseObject(mapper, reply);
        String s = node == null ? null : JsonExtract.string(node, "summary", "");
        if (s == null || s.isBlank()) {
            log.debug("摘要输出无法解析，本次跳过。片段：{}", clip(reply));
            return null;
        }
        return s.strip();
    }

    private static String clip(String s) {
        if (s == null) {
            return "";
        }
        String t = s.replace('\n', ' ').strip();
        return t.length() <= 160 ? t : t.substring(0, 160) + "…";
    }
}
