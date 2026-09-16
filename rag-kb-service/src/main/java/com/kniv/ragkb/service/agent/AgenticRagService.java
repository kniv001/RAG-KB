package com.kniv.ragkb.service.agent;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.domain.dto.CachedAnswer;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.cache.CacheService;
import com.kniv.ragkb.service.config.RagProperties;
import com.kniv.ragkb.service.retrieve.Retriever;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/**
 * Agentic RAG：规划 → 检索 → 评估 →（不足则换查询再来）→ 生成。
 *
 * <p><b>为什么本质仍是 RAG</b>：agent 只是控制流，<b>事实来源始终只有检索到的资料</b>。
 * 三条硬约束保证这一点：
 * <ol>
 *   <li>可用动作只有「检索」—— 不接外部工具、不联网、不写文件</li>
 *   <li>评估阶段判「资料不足」时如实说不知道，而不是让模型自由发挥</li>
 *   <li>最终回答逐条标注来源编号，可回溯到具体文档与块号</li>
 * </ol>
 *
 * <p><b>为什么用提示词驱动而不是原生 tool calling</b>：本地 qwen3:8b 的原生工具调用
 * 不稳定（时而漏参数、时而把工具名写错），而「让模型输出一段 JSON」这件事它做得很好。
 * 代价是要写健壮的 JSON 抽取（见 {@link JsonExtract}），收益是行为可控、出错好定位。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class AgenticRagService {

    private static final String PLAN_PROMPT = """
            你是知识库检索规划器。把用户的问题拆成 1~3 个用于检索的查询。

            硬性要求：
            1. 每个查询必须自包含，不能出现「它」「这个」「上面提到的」这类指代 —— 检索时没有对话上下文。
            2. 查询用词要贴近资料里可能出现的说法，不要改写成抽象概念。
            3. 问题简单就给 1 个查询，不要为了凑数硬拆。
            4. 只输出 JSON，不要解释、不要加代码块标记。

            输出格式：{"queries":["查询一","查询二"]}""";

    /**
     * 评估提示词刻意写得「宽容」。
     *
     * <p>实测教训：初版判据里写了「包含回答问题所需的关键事实即为够」，但小模型把它
     * 理解成了「必须有权威出处」—— 明明资料里的实测证据足以回答问题，它却因为
     * 「文档没有写明政策文件名称」判了不够，于是白跑两轮、多花 120 秒。
     * 所以这里显式禁止那类标准。
     */
    private static final String ASSESS_PROMPT = """
            你是资料充分性评估器。判断给出的资料能否支撑回答用户的问题。

            判断标准：
            - 只要资料里有能用来回答的**具体内容**（事实、现象、步骤、数据、结论、甚至间接线索），就判「够」。
            - 只有当资料完全没涉及问题主题，或通篇只是泛泛提及而无任何可用内容时，才判「不够」。

            明确禁止的判「不够」理由：
            - 「资料没有给出官方出处 / 权威文件名称」
            - 「资料没有明确写明原因 / 政策依据」
            - 「资料不够全面 / 不够详细」
            这些都不是「不够」。用户要的是能回答问题，不是要一份官方文件。

            只输出 JSON，不要解释：
            {"enough":true,"reason":"一句话理由","missing":"若不够，说明还缺什么"}""";

    /**
     * 回答用的系统提示词。
     *
     * <p>核心是<b>三段式</b>：有资料就严格按资料答；没资料也不要甩一句「没有」了事，
     * 而是先声明知识库没有、再用通用知识回答并标注清楚；纯对话问题不受知识库约束。
     *
     * <p>第 2 条为什么必须写死「不要只回一句」：原先没资料时是直接返回硬编码的
     * 「资料中没有相关内容。」，连模型都不调 —— 用户永远只能拿到那 9 个字，
     * 既不知道知识库里有什么接近的，也拿不到任何可用信息。
     *
     * <p>第 5 条同样是必需的，不是礼貌措辞：没有它，前两条的偏置会强到让模型
     * 拒绝一切「本次对话之前说过什么」的问题 —— 实测它对着明明注入进去的
     * 【更早的对话】照样回「资料中没有相关内容」，因为规则 2 直接授权了那句兜底。
     *
     * <p>标注要求写得这么硬，是因为这是这套系统唯一的信任边界：
     * 用户必须一眼分得清哪句来自自己的资料、哪句是模型的通用知识。
     */
    private static final String ANSWER_SYSTEM = """
            你是个人知识库助手。回答分三种情况处理：

            【一】知识库检索到了相关资料
            严格依据【参考资料】回答，不得编造资料里没有的内容。引用处用 [编号] 标注来源。

            【二】知识库没有相关资料
            不要只回一句「资料中没有相关内容」就结束 —— 那样对用户毫无帮助。
            按这个结构回答：
              ① 第一句先说明知识库中没有这方面的资料；
              ② 然后基于你自己的通用知识作答，尽量具体、有条理；
              ③ 明确标注这部分是通用知识、并非来自用户的知识库（例如另起一行写
                 「以下为通用知识，未引用你的知识库」），让用户一眼分得清。
            通用知识里没有把握的内容直说不知道，不要为了显得完整而编。

            【三】问题与知识库无关（闲聊、问你是谁、问当前对话说过什么等）
            直接正常回答，不用套用上面的知识库约束。

            通用要求：
            - 用中文，简洁准确；涉及要点时用条目列出。
            - 引用【参考资料】的地方用 [编号] 标注；通用知识部分不要标 [编号]，
              否则会让人误以为有出处。
            - 若问题问的是「本次对话之前说过什么」或需要靠上下文解析指代，
              依据【更早的对话】回答 —— 这类问题属于情况【三】，不是【二】。""";

    /**
     * 规划输出的形状。交给提供方做语法约束，模型便无法产出这个形状之外的任何东西 ——
     * 连「好的，我来帮你拆解」这种开场白都发不出来。
     */
    private static final String PLAN_SCHEMA = """
            {"type":"object","properties":{"queries":{"type":"array","items":{"type":"string"}}},"required":["queries"]}""";

    /** 评估输出的形状。只强制 enough，因为它是唯一被程序读取的字段。 */
    private static final String ASSESS_SCHEMA = """
            {"type":"object","properties":{"enough":{"type":"boolean"},"reason":{"type":"string"},"missing":{"type":"string"}},"required":["enough"]}""";

    private static final double TEMPERATURE = 0.2;

    /**
     * 思考片段的合并阈值：攒够这么多字才推一次 SSE。
     *
     * <p>逐 token 推会产生几千个事件（实测一段思考 1700~5500 字），而每个事件都要
     * 单独过一遍 AES-GCM 加密再发出去。思考的用途只是「让用户看到在动」，
     * 不需要逐字。60 字约合 40 个 token，在这个模型的生成速度下约半秒一批。
     */
    private static final int THINKING_FLUSH_CHARS = 60;

    private final ProviderRegistry providers;
    private final Retriever retriever;
    private final RagProperties props;
    private final ObjectMapper mapper;
    private final CacheService cache;

    /** 一次问答的产物。queries 记录实际检索过哪些查询，便于事后复盘检索质量。 */
    public record AgentResult(String answer, List<ChunkHit> sources, int rounds, List<String> queries) {
    }

    /**
     * 进提示词的三层历史材料。分三层是因为它们的<b>可靠性递减</b>：
     *
     * <ol>
     *   <li>{@code turns} 最近若干轮的<b>原文</b> —— 最可靠，全文，无检索参与</li>
     *   <li>{@code excerpt} 更早轮次里<b>向量召回</b>出来的片段 —— 精确但会漏</li>
     *   <li>{@code summary} 覆盖全部历史的<b>滚动摘要</b> —— 很粗但不会完全丢</li>
     * </ol>
     *
     * <p>三者是互补的：中间那层负责细节，最下面那层是漏召时的兜底。
     * 单独任何一层都不够 —— 光有原文撑不起长对话，光有检索会断片，光有摘要又太糊。
     */
    public record HistoryContext(List<ChatMessage> turns, String excerpt, String summary) {
        public static final HistoryContext EMPTY = new HistoryContext(List.of(), null, null);
    }

    // ---------------- Agentic 模式 ----------------

    public AgentResult agentic(String question, String modelRef, String mode, String docId,
                               Consumer<AgentEvent> onEvent) {
        return agentic(question, modelRef, mode, docId, HistoryContext.EMPTY, onEvent);
    }

    /**
     * @param hist 三层历史材料，见 {@link HistoryContext}。规划与回答都会用到 ——
     *             没有它，「那它呢」这类追问无法解析指代，检索会跑偏。
     */
    public AgentResult agentic(String question, String modelRef, String mode, String docId,
                               HistoryContext hist, Consumer<AgentEvent> onEvent) {
        RagProperties.Agent cfg = props.getAgent();
        ProviderRegistry.Ref ref = providers.resolveChat(modelRef);
        // 规划与评估走小模型（若已配置）：这两步只需输出 JSON，却占了大部分耗时
        ProviderRegistry.Ref utility = utilityRef(ref);

        Map<Long, ChunkHit> collected = new LinkedHashMap<>();
        List<String> tried = new ArrayList<>();
        List<String> queries = plan(utility, question, null, null, hist);

        int round = 0;
        boolean enough = false;
        for (round = 1; round <= cfg.getMaxRounds(); round++) {
            // ① 规划
            onEvent.accept(AgentEvent.plan(round, queries));

            // ② 检索
            for (String q : queries) {
                List<ChunkHit> hits = retriever.search(q, mode, docId, cfg.getTopKPerQuery());
                for (ChunkHit h : hits) {
                    collected.putIfAbsent(h.getId(), h);
                }
                onEvent.accept(AgentEvent.retrieve(round, q, hits.size(), sourceNames(hits)));
            }
            tried.addAll(queries);

            List<ChunkHit> contexts = rank(collected.values());

            // ③ 评估
            Assess assess = assess(utility, question, contexts);
            enough = assess.enough;
            onEvent.accept(AgentEvent.assess(round, enough, assess.reason, assess.missing));
            if (enough || round == cfg.getMaxRounds()) {
                break;
            }

            // ④ 不够 → 换查询再来一轮
            queries = plan(utility, question, assess.missing, tried, hist);
            if (queries.isEmpty()) {
                break;
            }
        }

        List<ChunkHit> contexts = rank(collected.values());
        String answer = answer(ref, question, contexts, hist, onEvent);
        return new AgentResult(answer, contexts, round, tried);
    }

    // ---------------- 经典单轮模式（对照与降级） ----------------

    public AgentResult classic(String question, String modelRef, String mode, String docId,
                               Consumer<AgentEvent> onEvent) {
        return classic(question, modelRef, mode, docId, HistoryContext.EMPTY, onEvent);
    }

    public AgentResult classic(String question, String modelRef, String mode, String docId,
                               HistoryContext hist, Consumer<AgentEvent> onEvent) {
        ProviderRegistry.Ref ref = providers.resolveChat(modelRef);
        List<ChunkHit> hits = retriever.search(question, mode, docId, null);
        onEvent.accept(AgentEvent.retrieve(1, question, hits.size(), sourceNames(hits)));
        String answer = answer(ref, question, hits, hist, onEvent);
        return new AgentResult(answer, hits, 1, List.of(question));
    }

    // ---------------- 三步 ----------------

    /** 解析规划/评估用的模型；未配置或配置无效时回退主模型，不让整条链路挂掉。 */
    private ProviderRegistry.Ref utilityRef(ProviderRegistry.Ref main) {
        String u = props.getAgent().getUtilityModel();
        if (u == null || u.isBlank()) {
            return main;
        }
        try {
            return providers.resolveChat(u);
        } catch (Exception e) {
            log.warn("utility-model 配置无效（{}），回退主模型：{}", u, e.getMessage());
            return main;
        }
    }

    /**
     * 走「只要 JSON」的通路 —— 规划与评估的共用入口。
     *
     * <p>这两步的产物是<b>数据</b>不是<b>文本</b>：一个查询数组、一个布尔值。
     * 让模型为此先写几千 token 的推理，是这条链路上最大的一笔浪费。
     */
    private String json(ProviderRegistry.Ref ref, String system, String user,
                        double temperature, String schema) {
        if (props.getAgent().isStructuredOutput()) {
            return providers.chatJson(ref,
                    List.of(ChatMessage.system(system), ChatMessage.user(user)),
                    temperature, schema).content();
        }
        return providers.chat(ref,
                List.of(ChatMessage.system(system), ChatMessage.user(user)),
                temperature).content();
    }

    private List<String> plan(ProviderRegistry.Ref ref, String question, String missing,
                              List<String> tried, HistoryContext hist) {
        StringBuilder user = new StringBuilder();
        List<ChatMessage> history = hist.turns();
        // 三层按可靠性递减排列：摘要在最前（最粗），原文在最后（最准）。
        // 指代对象完全可能落在最近窗口之外 —— 靠下面的召回与摘要才能解出来。
        if (hist.summary() != null && !hist.summary().isBlank()) {
            user.append("对话背景（全部历史的摘要）：\n").append(hist.summary()).append("\n\n");
        }
        if (hist.excerpt() != null && !hist.excerpt().isBlank()) {
            user.append("更早的对话（由检索召回）：\n").append(hist.excerpt()).append("\n\n");
        }
        // 把最近的对话带上：没有它，「那它呢」「上面说的第二点」这类追问
        // 会被当成独立问题去检索，结果必然跑偏
        if (history != null && !history.isEmpty()) {
            user.append("最近的对话：\n");
            for (ChatMessage m : history) {
                user.append("assistant".equals(m.role()) ? "助手：" : "用户：")
                        .append(clip(m.content(), 200)).append('\n');
            }
            user.append('\n');
        }
        user.append("用户问题：").append(question);
        if (missing != null && !missing.isBlank()) {
            user.append("\n\n上一轮检索后仍缺少：").append(missing);
        }
        if (tried != null && !tried.isEmpty()) {
            user.append("\n\n已经用过的查询（请换不同说法，不要重复）：")
                    .append(String.join("、", tried));
        }

        try {
            String reply = json(ref, PLAN_PROMPT, user.toString(), 0.2, PLAN_SCHEMA);
            JsonNode node = JsonExtract.parseObject(mapper, reply);
            List<String> queries = JsonExtract.stringArray(node, "queries", props.getAgent().getQueriesPerRound());
            if (!queries.isEmpty()) {
                return queries;
            }
            log.warn("规划未产出可解析的查询，降级为直接用原问题。模型输出片段：{}", truncate(reply));
        } catch (Exception e) {
            log.warn("规划失败，降级为直接用原问题：{}", e.getMessage());
        }
        return List.of(question);
    }

    private record Assess(boolean enough, String reason, String missing) {
    }

    private Assess assess(ProviderRegistry.Ref ref, String question, List<ChunkHit> contexts) {
        if (contexts.isEmpty()) {
            return new Assess(false, "没有检索到任何资料", "知识库中缺少该主题的内容");
        }
        StringBuilder user = new StringBuilder("用户问题：").append(question).append("\n\n已有资料：\n");
        for (int i = 0; i < contexts.size(); i++) {
            ChunkHit h = contexts.get(i);
            user.append('[').append(i + 1).append("] ").append(h.getDocName())
                    .append(" 第 ").append(h.getSeq()).append(" 块：")
                    .append(clip(h.getContent(), 220)).append('\n');
        }
        try {
            String reply = json(ref, ASSESS_PROMPT, user.toString(), 0.0, ASSESS_SCHEMA);
            JsonNode node = JsonExtract.parseObject(mapper, reply);
            if (node != null) {
                return new Assess(
                        JsonExtract.bool(node, "enough", true),
                        JsonExtract.string(node, "reason", ""),
                        JsonExtract.string(node, "missing", ""));
            }
            log.warn("评估输出无法解析，保守判定为「够」。模型输出片段：{}", truncate(reply));
        } catch (Exception e) {
            log.warn("评估失败，保守判定为「够」：{}", e.getMessage());
        }
        // 评估失败时判「够」而不是「不够」：多一轮检索要再等几十秒，
        // 而多数情况下第一轮的资料已足够，让用户白等更糟
        return new Assess(true, "评估不可用，默认按资料充分处理", "");
    }

    private String answer(ProviderRegistry.Ref ref, String question, List<ChunkHit> contexts,
                          HistoryContext hist, Consumer<AgentEvent> onEvent) {
        List<ChatMessage> history = hist.turns();
        String historyExcerpt = hist.excerpt();
        String convSummary = hist.summary();
        boolean hasExcerpt = historyExcerpt != null && !historyExcerpt.isBlank();
        boolean hasSummary = convSummary != null && !convSummary.isBlank();
        if (contexts.isEmpty() && !hasExcerpt && !hasSummary
                && !props.getAgent().isAnswerWithoutContext()) {
            // 快速兜底：不调模型，直接返回一句「资料中没有相关内容」。
            // 默认<b>关闭</b> —— 这句话对用户毫无帮助，而且它连模型都不调，
            // 用户永远只能拿到那 9 个字。见 ANSWER_SYSTEM 的情况【二】。
            String fallback = "资料中没有相关内容。";
            onEvent.accept(AgentEvent.answerToken(fallback));
            return fallback;
        }

        StringBuilder user = new StringBuilder();
        // 三层历史按可靠性递减排：摘要最粗在最前，召回片段居中，最新原文由 messages 承担。
        // 【参考资料】是事实依据，让它紧挨着【问题】——「lost in the middle」下这个位置最不容易被漏掉
        if (hasSummary) {
            user.append("【对话背景】（全部历史的摘要，用于理解上下文与指代；粒度粗，细节以【更早的对话】与【参考资料】为准）\n")
                    .append(convSummary).append("\n\n");
        }
        if (hasExcerpt) {
            // 标签要写清楚它「能用来干什么」：只写「不作为事实来源」的话，
            // 模型连「你刚才说的第二点是什么」这类问题都会拒绝回答
            user.append("【更早的对话】（本次对话早前的内容，由检索召回）\n")
                    .append(historyExcerpt)
                    .append("\n（以上用于理解指代与上下文；问「之前说过什么」时依据它回答，"
                            + "问知识内容时仍以【参考资料】为准）\n\n");
        }
        if (contexts.isEmpty()) {
            // 不在这里给处置指令 —— 该怎么答由 ANSWER_SYSTEM 的三段式统一决定。
            // 之前这里写死了「若问的是知识内容，回答『资料中没有相关内容』」，
            // 等于把模型的嘴堵上，用户只能拿到那句话。
            user.append("【参考资料】\n（本次未检索到与问题相关的资料。）\n\n");
        } else {
            user.append("【参考资料】\n");
            for (int i = 0; i < contexts.size(); i++) {
                ChunkHit h = contexts.get(i);
                user.append('[').append(i + 1).append("] 来源：").append(h.getDocName())
                        .append("（第 ").append(h.getSeq()).append(" 块）\n")
                        .append(h.getContent()).append("\n\n");
            }
        }
        user.append("【问题】\n").append(question);
        // 排查用：模型答「资料中没有」时，先分清是没检索到、还是检索到了它不用
        log.debug("回答注入：资料 {} 段 / 召回片段 {} 字 / 摘要 {} 字 / 近轮 {} 条",
                contexts.size(),
                hasExcerpt ? historyExcerpt.length() : 0,
                hasSummary ? convSummary.length() : 0,
                history == null ? 0 : history.size());

        // ---- 回答缓存 ----
        // 键里含「上下文哈希」与「历史哈希」：资料改了或对话历史变了，键就变，
        // 因此永远不存在返回陈旧答案的窗口。这也是清空缓存只影响空间、不影响正确性的原因。
        List<String> contents = new ArrayList<>(contexts.size());
        for (ChunkHit h : contexts) {
            contents.add(h.getContent());
        }
        List<String> historyLines = new ArrayList<>();
        if (history != null) {
            for (ChatMessage m : history) {
                historyLines.add(m.role() + ":" + m.content());
            }
        }
        if (historyExcerpt != null && !historyExcerpt.isBlank()) {
            // 召回的旧轮次必须进键：召回结果变了，答案就可能变，
            // 不进键的话会命中一条基于不同上下文算出来的陈旧答案
            historyLines.add("excerpt:" + historyExcerpt);
        }
        if (hasSummary) {
            // 摘要同理 —— 它在后台异步更新，不进键的话摘要变了答案却还命中旧的
            historyLines.add("summary:" + convSummary);
        }
        String cacheKey = CacheService.answerKey(question,
                CacheService.contextHash(contents), CacheService.historyHash(historyLines),
                ref.providerId(), ref.model(), TEMPERATURE);

        CachedAnswer hit = cache.getAnswer(cacheKey);
        if (hit != null && hit.getAnswer() != null && !hit.getAnswer().isBlank()) {
            log.info("回答缓存命中，跳过生成（{} 字）", hit.getAnswer().length());
            // 缓存命中时一次性推出整段：客户端渲染是瞬时的，
            // 再逐字模拟反而增加无谓往返
            onEvent.accept(AgentEvent.answerToken(hit.getAnswer()));
            return hit.getAnswer();
        }

        List<ChatMessage> messages = new ArrayList<>();
        messages.add(ChatMessage.system(ANSWER_SYSTEM));
        // 历史放在资料之前：事实依据仍来自资料，历史只用来理解指代
        if (history != null) {
            messages.addAll(history);
        }
        messages.add(ChatMessage.user(user.toString()));

        StringBuilder out = new StringBuilder();
        StringBuilder thinkBuf = new StringBuilder();
        Consumer<String> onThinking = thinkBuf::append;
        if (props.getAgent().isStreamThinking()) {
            // 攒够一批再推，见 THINKING_FLUSH_CHARS
            onThinking = piece -> {
                thinkBuf.append(piece);
                if (thinkBuf.length() >= THINKING_FLUSH_CHARS) {
                    onEvent.accept(AgentEvent.thinking(thinkBuf.toString()));
                    thinkBuf.setLength(0);
                }
            };
        }
        providers.chatStream(ref, messages, TEMPERATURE,
                piece -> {
                    out.append(piece);
                    onEvent.accept(AgentEvent.answerToken(piece));
                },
                onThinking);
        if (thinkBuf.length() > 0) {
            onEvent.accept(AgentEvent.thinking(thinkBuf.toString()));
        }

        if (out.length() > 0) {
            String sourcesJson;
            try {
                sourcesJson = mapper.writeValueAsString(
                        contexts.stream().map(this::sourceOf).toList());
            } catch (Exception e) {
                sourcesJson = "[]";
            }
            cache.putAnswer(cacheKey, question, out.toString(),
                    ref.providerId(), ref.model(), sourcesJson);
        }
        return out.toString();
    }

    private Map<String, Object> sourceOf(ChunkHit h) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("docId", h.getDocId());
        m.put("docName", h.getDocName());
        m.put("seq", h.getSeq());
        m.put("distance", h.getDistance());
        m.put("score", h.getScore());
        return m;
    }

    // ---------------- 工具方法 ----------------

    /** 排序：有距离的按距离升序在前，关键词命中的按命中数降序在后。 */
    private List<ChunkHit> rank(java.util.Collection<ChunkHit> hits) {
        List<ChunkHit> list = new ArrayList<>(hits);
        list.sort(Comparator
                .comparing((ChunkHit h) -> h.getDistance() == null ? 1 : 0)
                .thenComparing(h -> h.getDistance() == null ? 0.0 : h.getDistance())
                .thenComparing(h -> h.getHits() == null ? 0 : -h.getHits()));
        int cap = props.getAgent().getMaxContexts();
        return list.size() > cap ? new ArrayList<>(list.subList(0, cap)) : list;
    }

    private List<String> sourceNames(List<ChunkHit> hits) {
        List<String> names = new ArrayList<>();
        for (ChunkHit h : hits) {
            names.add(h.getDocName() + "#" + h.getSeq());
            if (names.size() >= 5) {
                break;
            }
        }
        return names;
    }

    private static String clip(String s, int max) {
        if (s == null) {
            return "";
        }
        String t = s.replace('\n', ' ').strip();
        return t.length() <= max ? t : t.substring(0, max) + "…";
    }

    private static String truncate(String s) {
        return clip(s, 200);
    }
}
