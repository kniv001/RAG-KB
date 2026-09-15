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

    private static final String ANSWER_SYSTEM = """
            你是严谨的个人知识库助手。

            规则：
            1. 只依据【参考资料】回答，不得编造资料中没有的内容。
            2. 参考资料无法回答时，直接说明「资料中没有相关内容」，不要猜测。
            3. 用中文回答，简洁准确；涉及要点时用条目列出。
            4. 引用了某段资料的地方，用 [编号] 标注来源。""";

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

    // ---------------- Agentic 模式 ----------------

    public AgentResult agentic(String question, String modelRef, String mode, String docId,
                               Consumer<AgentEvent> onEvent) {
        return agentic(question, modelRef, mode, docId, List.of(), onEvent);
    }

    /**
     * @param history 最近若干轮对话（时间正序）。规划与回答都会用到 ——
     *                没有它，「那它呢」这类追问无法解析指代，检索会跑偏。
     */
    public AgentResult agentic(String question, String modelRef, String mode, String docId,
                               List<ChatMessage> history, Consumer<AgentEvent> onEvent) {
        RagProperties.Agent cfg = props.getAgent();
        ProviderRegistry.Ref ref = providers.resolveChat(modelRef);
        // 规划与评估走小模型（若已配置）：这两步只需输出 JSON，却占了大部分耗时
        ProviderRegistry.Ref utility = utilityRef(ref);

        Map<Long, ChunkHit> collected = new LinkedHashMap<>();
        List<String> tried = new ArrayList<>();
        List<String> queries = plan(utility, question, null, null, history);

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
            queries = plan(utility, question, assess.missing, tried, history);
            if (queries.isEmpty()) {
                break;
            }
        }

        List<ChunkHit> contexts = rank(collected.values());
        String answer = answer(ref, question, contexts, history, onEvent);
        return new AgentResult(answer, contexts, round, tried);
    }

    // ---------------- 经典单轮模式（对照与降级） ----------------

    public AgentResult classic(String question, String modelRef, String mode, String docId,
                               Consumer<AgentEvent> onEvent) {
        return classic(question, modelRef, mode, docId, List.of(), onEvent);
    }

    public AgentResult classic(String question, String modelRef, String mode, String docId,
                               List<ChatMessage> history, Consumer<AgentEvent> onEvent) {
        ProviderRegistry.Ref ref = providers.resolveChat(modelRef);
        List<ChunkHit> hits = retriever.search(question, mode, docId, null);
        onEvent.accept(AgentEvent.retrieve(1, question, hits.size(), sourceNames(hits)));
        String answer = answer(ref, question, hits, history, onEvent);
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
                              List<String> tried, List<ChatMessage> history) {
        StringBuilder user = new StringBuilder();
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
                          List<ChatMessage> history, Consumer<AgentEvent> onEvent) {
        if (contexts.isEmpty()) {
            String fallback = "资料中没有相关内容。";
            onEvent.accept(AgentEvent.answerToken(fallback));
            return fallback;
        }

        StringBuilder user = new StringBuilder("【参考资料】\n");
        for (int i = 0; i < contexts.size(); i++) {
            ChunkHit h = contexts.get(i);
            user.append('[').append(i + 1).append("] 来源：").append(h.getDocName())
                    .append("（第 ").append(h.getSeq()).append(" 块）\n")
                    .append(h.getContent()).append("\n\n");
        }
        user.append("【问题】\n").append(question);

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
