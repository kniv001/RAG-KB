package com.kniv.ragkb.service.chat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.MessageMapper;
import com.kniv.ragkb.domain.entity.Message;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.JsonExtract;
import com.kniv.ragkb.service.config.GpuGate;
import com.kniv.ragkb.service.config.RagProperties;
import com.kniv.ragkb.service.index.EmbeddingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 轮次笔记：把一轮对话改写成自包含的一段话，索引的是它而不是原文。
 *
 * <p><b>解决什么</b>：单条 message 不是好的检索单元。
 * <ul>
 *   <li>单独索引用户那句 —— 常常很短、带指代（「那它呢」），当检索键很差</li>
 *   <li>单独索引助手那句 —— 脱离问题可能没头没尾（「它的失效策略有三点…」）</li>
 *   <li>助手回答里夹着「根据参考资料[1]」「以下为通用知识」这类包装，
 *       检索时稀释向量、阅读时是噪音</li>
 * </ul>
 *
 * <p><b>谁做改写</b>：对话模型，不是向量模型。向量模型只负责把改好的笔记
 * 变成可比距离的点 —— 它不"造文档"，也不会消解指代或提炼结论。
 *
 * <p><b>只改写窗口之外的轮次</b>：窗口内的原文本来就在提示词里，改写它们
 * 是重复劳动。这与滚动摘要的 {@code windowMessages} 是同一个边界，
 * 所以一条消息要么"在窗口里（原文）"、要么"在窗口外（笔记）"，不会两头都出现。
 *
 * <p><b>必须让路给用户</b>：每轮改写是一次 LLM 调用。实测本机只有一个推理槽，
 * 后台任务跑多久用户就等多久（3091ms 的后台任务让用户等 3095ms），
 * 所以进模型前先过 {@link GpuGate}。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class TurnDocService {

    private static final String PROMPT = """
            你在把一轮对话改写成一段**可独立检索**的笔记。
            这段笔记之后会被单独拿出来做向量检索，所以它必须不依赖上下文就能读懂。

            要求：
            1. 消解指代：「它」「这个」「上面说的」必须换成具体所指 —— 检索时没有上下文。
            2. 去掉包装：不要「根据参考资料[1]」「以下为通用知识」这类话，直接写结论。
            3. **保留原文的全部实质内容** —— 数字、名称、步骤、每一条结论都要留着。
               这一步是「去掉上下文依赖」，**不是概括**。
               只写「有 Meta、Root、branch、Leaf 四种页」而丢掉了每种页是干什么的，
               就是失败的改写。
            4. 用户明确提过的约束原样保留，不要替他改写措辞。
            5. **note 里不要出现「问：」这一行**，只写改写后的说明本身。
               实测教训：让模型输出「问：… 答：…」时，答的部分改得很干净，
               问的那行却照抄了用户带指代的原话（「那它们之间是什么关系？」）——
               而整段都要进向量，带着指代就是在稀释检索信号。
               去掉这一行最简单：答的部分本来就是自包含的，而检索靠的是
               具体名词（B-tree / Root Page / 缓存键），不是那个问句。
            6. 长度与原文相当即可 —— 砍掉的应当只是包装话术，不是内容。

            只输出 JSON：{"topic":"主题名，不超过10字","note":"改写后的说明"}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"topic":{"type":"string"},"note":{"type":"string"}},
             "required":["topic","note"]}""";

    private final MessageMapper messages;
    private final EmbeddingService embedding;
    private final ProviderRegistry providers;
    private final RagProperties props;
    private final GpuGate gpuGate;
    private final ObjectMapper mapper;

    private final ExecutorService pool = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "turn-doc");
        t.setDaemon(true);
        return t;
    });

    /** 同一会话同一时刻只处理一次，避免连续追问时堆积 */
    private final Set<String> inFlight = ConcurrentHashMap.newKeySet();

    /**
     * 触发一次异步改写。不满足条件就什么都不做 —— 每轮问答后都会调用，必须廉价。
     */
    public void maybeUpdate(String convId) {
        RagProperties.TurnDoc cfg = props.getTurnDoc();
        if (!cfg.isEnabled() || convId == null || convId.isBlank()) {
            return;
        }
        try {
            // 窗口之外最新的那一条。窗口内的原文还要给模型看，不需要笔记。
            Long upto = messages.idAtOffset(convId, cfg.getWindowMessages());
            if (upto == null) {
                return;   // 消息还没多到溢出窗口
            }
            List<Message> pending = messages.turnsWithoutNote(convId, upto, cfg.getBatchMessages());
            if (pending.isEmpty()) {
                return;   // 这一批已经改完了
            }
            if (!inFlight.add(convId)) {
                return;   // 上一次还没跑完
            }
            pool.submit(() -> {
                try {
                    // 等用户静默再进模型。本机只有一个推理槽，后台任务跑多久用户就等多久。
                    if (!gpuGate.awaitIdle()) {
                        return;
                    }
                    int done = 0;
                    for (Message answer : pending) {
                        if (rewrite(convId, answer)) {
                            done++;
                        }
                    }
                    log.debug("会话 {} 改写 {} 轮为笔记", convId, done);
                } catch (Exception e) {
                    log.warn("轮次改写失败（不影响问答）：{}", e.getMessage());
                } finally {
                    inFlight.remove(convId);
                }
            });
        } catch (Exception e) {
            log.warn("轮次改写调度失败（不影响问答）：{}", e.getMessage());
        }
    }

    /** @return 是否写成功 */
    private boolean rewrite(String convId, Message answer) {
        Message question = messages.questionBefore(convId, answer.getId());
        if (question == null) {
            // 没有对应的提问（比如用户消息被删了），那就别改了 —— 单看回答改不出好东西
            messages.markNoteSkipped(answer.getId());
            return false;
        }

        try {
            String user = "用户问：\n" + clip(question.getContent(), 400)
                    + "\n\n助手答：\n" + clip(answer.getContent(), 1200);
            ProviderRegistry.Ref ref = providers.resolveChat(props.getAgent().getUtilityModel());
            String reply = providers.chatJson(ref,
                    List.of(ChatMessage.system(PROMPT), ChatMessage.user(user)),
                    0.2, SCHEMA).content();

            JsonNode node = JsonExtract.parseObject(mapper, reply);
            String topic = node == null ? "" : JsonExtract.string(node, "topic", "");
            String note = node == null ? "" : JsonExtract.string(node, "note", "");

            // 形状合法但内容是敷衍的也要拦下（实测模型对"很杂"的输入会回 "..."）
            if (!usable(topic, 2) || !usable(note, 20)) {
                log.debug("第 {} 条的改写不可用（topic=「{}」，note {} 字），跳过",
                        answer.getId(), topic, note.length());
                messages.markNoteSkipped(answer.getId());
                return false;
            }

            // 用笔记重新算向量 —— 一次会话消息量不大，这点开销可以忽略
            float[] vec = embedding.embedOne(note);
            messages.setNote(answer.getId(), note.strip(), topic.strip(),
                    VectorTypeHandler.toLiteral(vec), embedding.modelColumn());
            return true;
        } catch (Exception e) {
            log.debug("第 {} 条改写失败：{}", answer.getId(), e.getMessage());
            return false;
        }
    }

    /** 与主题树那边同一个判据：名字里至少要有一个实义字符 */
    private static boolean usable(String s, int minLen) {
        if (s == null) {
            return false;
        }
        String t = s.strip();
        return t.length() >= minLen && t.codePoints().anyMatch(Character::isLetterOrDigit);
    }

    private static String clip(String s, int max) {
        if (s == null) {
            return "";
        }
        String t = s.replace('\n', ' ').strip();
        return t.length() <= max ? t : t.substring(0, max) + "…";
    }
}
