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

import java.util.ArrayList;
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

    /**
     * 摘要的**形状**是实测定的：一行一条，不是一段连贯文字。
     *
     * <p>2026-09-18 的「记忆当检索」实验（拿决策台账当记忆库、11 问 × 5 组）量到：
     * **一行一条的索引本身就承载了大半** —— 只给 22 行索引（2019 token，全量方案的 11%）
     * 就答对了 10/11，而召回正文只把关键短语命中从 19/39 抬到 25/39。原因很直接：
     * 那一行写的是**结论**，不是"更新了某个文件"。
     *
     * <p>所以这里把摘要从段落改成条目。**只改形状，不改语义** ——
     * 合并、增量、异步、上限都没动，方便出问题时判断是哪一处引起的。
     *
     * <p>条目再进一步写成**变化式**（「曾经 → 现在」），依据是同一晚的三次重复对照
     * （`tools/summary-shape-probe.py`，用例：旧摘要里有"块大小 600"和一条用户偏好，
     * 新对话把块大小改成 450）：
     * <pre>
     *   写法      有箭头   保住旧值   保住"没变化"的偏好
     *   状态式      0/3      0/3        1/3   ← 三次里丢两次
     *   变化式      3/3      3/3        3/3
     * </pre>
     * 关键不在箭头本身，而在**形状是个完整性约束**：强制"两边都要写"，就把"没变化"
     * 也逼成显式的 {@code — → 现状}；状态式的自由形式会让模型把不变的事实静默丢掉 ——
     * 而那恰恰是"用户要求记住、丢了就找不回来"的那一类。
     *
     * <p><b>已知失效模式</b>（用例C 长杂输入，n=1）：把两条并列事实压进一个箭头会走形，
     * 例如把"新文档保留 Markdown / 旧文档仍是一行标题"写成
     * 「旧文档结构：Markdown 保留 → 一行标题」。看到这类条目要当噪声处理。
     */
    private static final String PROMPT = """
            你是对话记忆的整理器。把「新增对话」并入「已有记忆」，输出**一行一条**的条目。

            **每条写「曾经 → 现在」**：箭头左边是之前的值，右边是**现在**的值。
            读的人按位置判断当前状态，不必回头比较两条。

            要求：
            1. **新增对话是新信息的唯一来源，每条结论都必须落到条目上** —— 已有条目为空时同样如此。
               任何情况下都不许返回空数组。
            2. 一条一个事实或结论，不要写成段落，不要写「用户问了…助手回答了…」的流水账。
            3. 保留具体信息：讨论的主题、得出的结论、用户明确表达过的偏好或约束、尚未解决的问题。
               用户明确要求记住的任何内容必须原样保留 —— 名字、代号、数字、约定、日期。
               这类信息一旦丢掉就再也找不回来，比主题概括重要得多。
            4. **没有变化的事实照样要写**，左边写「—」（如「偏好：— → 表格而非选项式」）。
               会话记忆里大部分本来就没变 —— 只写变化会把这些整段丢掉。
            5. 同一主题只留一条：已有条目被新增对话改变时**并进同一条**，写成「最初的值 → 现在的值」，
               不要层层叠加，也不要直接删掉（为什么变本身是有用的）。
            6. **代价、限制、反面结论要单独写出来**，不要跟好处挤在同一行。
            7. 最多 12 条，每条不超过 40 字。

            只输出 JSON：{"items":["主题：曾经 → 现在"]}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"items":{"type":"array","items":{"type":"string"}}},"required":["items"]}""";

    /** 条目硬上限：比提示词里的 12 宽松一档，只用来挡住模型偶发的刷屏 */
    private static final int MAX_ITEMS = 20;

    private final MessageMapper messages;
    private final ConversationMapper conversations;
    private final ProviderRegistry providers;
    private final RagProperties props;
    private final ObjectMapper mapper;
    private final com.kniv.ragkb.service.config.GpuGate gpuGate;

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
                    // 等用户静默再开始。本机只有一个推理槽（OLLAMA_NUM_PARALLEL=1），
                    // 实测后台任务跑 3091ms 时用户请求要等 3095ms —— 等满。
                    // 摘要晚做几分钟没有代价，让用户等有代价。
                    if (!gpuGate.awaitIdle()) {
                        return;
                    }
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
        user.append("已有条目：\n").append(old == null || old.isBlank() ? "（无，这是第一次）" : old)
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
        List<String> items = new ArrayList<>();
        if (node != null && node.path("items").isArray()) {
            for (JsonNode it : node.path("items")) {
                String s = it.asText("").strip();
                if (!s.isEmpty()) {
                    items.add(s);
                }
                if (items.size() >= MAX_ITEMS) {
                    break;
                }
            }
        }
        // 空数组**不能**覆盖已有摘要 —— 语法约束下 {"items":[]} 是最省的合法输出，
        // 而它在语义上等于「把记忆清空」。宁可这次不更新（下一批再试）。
        if (items.isEmpty()) {
            log.debug("摘要输出为空数组或无法解析，本次跳过（保留原摘要）。片段：{}", clip(reply));
            return null;
        }
        return String.join("\n", items);
    }

    private static String clip(String s) {
        if (s == null) {
            return "";
        }
        String t = s.replace('\n', ' ').strip();
        return t.length() <= 160 ? t : t.substring(0, 160) + "…";
    }
}
