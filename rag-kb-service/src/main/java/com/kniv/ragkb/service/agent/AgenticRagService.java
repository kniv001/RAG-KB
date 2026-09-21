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
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
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
     *
     * <p><b>2026-09-21 实测：宽容到它再也不判「不够」了 —— 三轮检索实际上从未发生过。</b>
     * 75 条真身记录（多跳 25 × 3 轮对照）**轮数全是 1**；
     * 另一次 21 题的采集里 {@code enough} **全是 true**。
     * 而 {@code maxRounds=2}、这条分支的全部意义就是"不够 → 换查询再来一轮"。
     *
     * <p>更麻烦的是**理由与判定会自相矛盾** —— 实测原文：
     * 「资料中<b>没有任何一条与 Prometheus 相关</b>…」⇒ {@code enough=true}。
     *
     * <p>⇒ 于是这一步**每题花一次模型调用（实测分段 ~1.8s），却从未改变过任何结果**。
     * 它现在等于纯开销。要留就得先让它判得准（难点见
     * {@code RagProperties.Agent.contractInCode} 的注释：难在"提到了主题"与
     * "能回答问题"这两件事的区分），要省就直接砍掉。
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
    /**
     * **给思考一个会终止的形状** —— 拼在 {@code ANSWER_SYSTEM} 末尾（默认关，见
     * {@code RagProperties.Agent#shapeThinking}）。
     *
     * <p>依据：decode 里 79~87% 的 token 是思考，而思考与正文**在同一条 decode 流里串行产生**。
     * 但回答路径的 {@code think} 不能关（{@code think:false} 会把推理转进正文污染输出）。
     * 所以只能约束**形状**：条目化 + 一个终止标记，写完就进正文。
     *
     * <p>为什么是形状而不是"请简短思考"：这条线上两次验证过 ——
     * 摘要的「变化式」和 plan/assess 的结构化输出，**形状本身就是约束**，祈使句弱得多。
     *
     * <p>注意这里的措辞在描述**思考该长什么样**，不是在替用户规定答案格式；
     * 三段式的回答要求不受影响。
     */
    private static final String THINK_SHAPE = """

            【关于思考过程】
            推理请写成**有编号的短清单**，最多 4 条、每条不超过 20 字，只写「用哪几块资料、结论是什么」。
            写完第 4 条（或更早想清楚时）**立刻换行开始写正文**，不要再展开、不要复述资料原文、
            不要自我辩论。清单里的编号沿用正文的引用编号即可。
            """;

    /** 回答路径的开关指纹。
     *
     *  <p>**必须进缓存键**：这几个开关都会改变"同一问题得到什么答案"，却都不改提示词文本
     *  （或只改用户消息），于是它们共用缓存条目 —— 表现是"开了开关但行为没变"，
     *  而且任何 A/B 都做不了（第二臂全命中第一臂）。
     *  这与「换解析器不会让解析缓存失效」「改提示词不会让答案缓存失效」是同一类坑，
     *  一天之内第三次了；所以这次不写手动版本号，直接**把开关状态算进键**。
     */
    private String answerPathTag() {
        return "2s=" + props.getAgent().isTwoStage()
                + ",ctx=" + props.getAgent().isCtxInPrompt()
                + ",shape=" + props.getAgent().isShapeThinking()
                + ",cic=" + props.getAgent().isContractInCode();
    }

    /** 回答用的系统提示：开关打开时拼上「思考形状」那一段。
     *
     *  <p>**三处必须都走这里**（发消息 + 两处预算估算）——
     *  只改发消息那处的话预算会低估，而低估的后果是提示词顶到窗口悬崖，
     *  Ollama 会把开头（也就是系统提示）整个丢掉。
     */
    private String answerSystem() {
        return props.getAgent().isShapeThinking() ? ANSWER_SYSTEM + THINK_SHAPE : ANSWER_SYSTEM;
    }

    /** 真正要发出去的那份系统提示。{@code hasContext} 为资料有无，只在开关打开时参与。
     *
     *  <p>**判定的依据就是"有没有资料"** —— 这正是代码能算、而模型每题要重推一遍的东西。
     *  没资料就是【丙】，有资料就是【乙】；【甲】代码判不了（应用没有闲聊检测），
     *  所以在【乙】那块里留了一句极短的例外。
     */
    private String answerSystem(boolean enough) {
        if (!props.getAgent().isContractInCode()) {
            return answerSystem();
        }
        return (enough ? CONTRACT_YI : CONTRACT_BING)
                + (props.getAgent().isShapeThinking() ? THINK_SHAPE : "");
    }

    private static final String ANSWER_SYSTEM = """
            你是个人知识库助手。**先判断问题属于哪一类**，再按下面对应的方式回答：

            【甲】与知识库无关 —— 闲聊、问你的身份、问本次对话之前说过什么、需要靠上下文解析指代。
            直接正常回答即可，**不要提「知识库中没有」**，也不要加「以下为通用知识」这类标注。
            被问身份时，说你是这个个人知识库的助手（底层由本地模型驱动）就够了，
            不必展开讲底层模型是哪家厂商的哪个型号。

            【乙】知识性问题，且【参考资料】里有内容
            严格依据资料回答，不得编造资料里没有的内容。引用处用 [编号] 标注来源。

            【丙】知识性问题，但知识库没有相关资料
            不要只回一句「资料中没有相关内容」就结束 —— 那样对用户毫无帮助。
            按这个结构回答：
              ① 第一句先说明知识库中没有这方面的资料。
                 若上面给了【知识库主题概览】，顺便点出库里**确实覆盖**的相关方向
                 （「没有 X，但有 Y 和 Z 两个方向」）—— 用户据此就知道该换个问法还是
                 该去补资料，比单说一句「没有」有用得多。
              ② 然后基于你自己的通用知识作答，尽量具体、有条理；
              ③ 明确标注这部分是通用知识、并非来自用户的知识库（例如另起一行写
                 「以下为通用知识，未引用你的知识库」），让用户一眼分得清。
            通用知识里没有把握的内容直说不知道，不要为了显得完整而编。

            【甲】排在最前是因为它最容易被误判：实测把「你是谁？」当成了知识性问题，
            回出「知识库中没有与『你是谁』相关的信息」再标注「以下为通用知识」，
            既绕又莫名其妙。

            **判断属于哪一类，一律以【参考资料】为准。**
            【知识库主题概览】只是「没资料时告诉用户库里还有哪些方向」的参考，
            它**被字数预算截断过、不是完整清单** —— 某方向不在概览里，不等于知识库没有。
            实测踩过：一道 G1 Mixed GC 的题**已经召回了四段 G1 资料**，
            模型却因为概览里没列 JVM 就判成【丙】、把有据可查的答案降级成「通用知识」。
            **资料在手就按【乙】答**，别让概览推翻参考资料。

            通用要求：
            - 用中文，简洁准确；涉及要点时用条目列出。
            - 引用【参考资料】的地方用 [编号] 标注；通用知识部分不要标 [编号]，
              否则会让人误以为有出处。
            - 数学公式一律用 LaTeX：行内用 $...$ 包起来，独立成行的用 $$...$$。
              前端会用 KaTeX 渲染它们。不要用纯文本凑公式 ——
              实测不特意要求时，模型会把余弦相似度写成「(A · B) / (||A|| × ...)」
              这种纯文本，那在前端只是一串字符，排版全乱。
            - 代码用围栏代码块并标注语言。""";

    /**
     * 「类型由代码给」版系统提示的两块 —— 见 {@code RagProperties.Agent.contractInCode}。
     *
     * <p>**与 {@code ANSWER_SYSTEM} 的差别只有一处：不再要求模型判断类别。**
     * 规则本身一条没删（引用标注、不得编造、丙的三段结构、LaTeX、代码块全都保留），
     * 所以两个臂的差别是**要不要做判定这件事**，不是"约束松了"。
     *
     * <p>通用要求单独抽出来，两块共用。
     */
    private static final String CONTRACT_COMMON = """
            通用要求：
            - 用中文，简洁准确；涉及要点时用条目列出。
            - 引用【参考资料】的地方用 [编号] 标注；通用知识部分不要标 [编号]，否则会让人误以为有出处。
            - 数学公式一律用 LaTeX：行内用 $...$ 包起来，独立成行的用 $$...$$。前端会用 KaTeX 渲染。
            - 代码用围栏代码块并标注语言。""";

    /** 代码判定为【乙】（**有**资料）。甲只能留一句极短例外 —— 应用没有闲聊检测。 */
    private static final String CONTRACT_YI = """
            你是个人知识库助手。

            **本次类型已由系统判定，直接照做，不要再去分析、判断或复述类别：**
            【乙】知识性问题，且【参考资料】里有内容。
            ⇒ 严格依据资料回答，不得编造资料里没有的内容。引用处用 [编号] 标注来源。
            ⇒ 资料里确实没给的具体值，直说资料没给，不要拿别处的值顶上。
            ⇒ **不要**提「知识库中没有」。
              （唯一例外：问题若与知识库无关 —— 闲聊、问你的身份、问本次对话之前说过什么 ——
               那就直接正常回答，同样不要提「知识库中没有」。）
            """ + CONTRACT_COMMON;

    /** 代码判定为【丙】（**没有**资料）。 */
    private static final String CONTRACT_BING = """
            你是个人知识库助手。

            **本次类型已由系统判定，直接照做，不要再去分析、判断或复述类别：**
            【丙】知识性问题，但知识库没有相关资料。
            ⇒ 不要只回一句「资料中没有相关内容」就结束，那样对用户毫无帮助。按这个结构回答：
              ① 第一句先说明知识库中没有这方面的资料。若上面给了【知识库主题概览】，
                 顺便点出库里**确实覆盖**的相关方向（「没有 X，但有 Y 和 Z 两个方向」）。
              ② 然后基于你自己的通用知识作答，尽量具体、有条理。
              ③ 明确标注这部分是通用知识、并非来自用户的知识库（例如另起一行写
                 「以下为通用知识，未引用你的知识库」）。
              通用知识里没有把握的内容直说不知道，不要为了显得完整而编。
            """ + CONTRACT_COMMON;

    /**
     * 规划输出的形状。交给提供方做语法约束，模型便无法产出这个形状之外的任何东西 ——
     * 连「好的，我来帮你拆解」这种开场白都发不出来。
     */
    private static final String PLAN_SCHEMA = """
            {"type":"object","properties":{"queries":{"type":"array","items":{"type":"string"}}},"required":["queries"]}""";

    /** 评估输出的形状。只强制 enough，因为它是唯一被程序读取的字段。 */
    private static final String ASSESS_SCHEMA = """
            {"type":"object","properties":{"enough":{"type":"boolean"},"reason":{"type":"string"},"missing":{"type":"string"}},"required":["enough"]}""";

    /**
     * **两段式的第一段**：把「想」挤进固定 schema。
     *
     * <p>为什么需要它（2026-09-20 直接读思考原文得到的证据）：回答路径的 decode 里
     * 84~86% 是思考，而思考的绝大部分是**同一件事换措辞说十几遍** ——
     * 最极端那题（「用一句话解释什么是限流」）：思考 3598 字 / 正文 58 字 = **62 倍**，
     * 里面是同一句定义的反复微调（"或者"↔"或"、有没有"一旦"、有没有 `[1][2]`）。
     *
     * <p>**所以逐字指标全都测不到它**（shingle 重合只有 2~19%，因为每遍用词都不同），
     * 而**形状约束也拦不住**（实测"最多 4 条"只压了 14% —— 它只是给每轮换个标签继续改）。
     *
     * <p>唯一能截断这个循环的是**语法约束**：{@code format} 下模型只能往固定格子里填，
     * **没有反复改措辞的余地**。这就是 plan/assess 已经验证过的机制（51.5s → 0.94s）。
     */
    private static final String ANALYSIS_PROMPT = """
            你是知识库问答的**分析器**。只做分析，**不要写回答**。

            1. 判断问题属于【甲】【乙】【丙】哪一类，以【参考资料】为准。
            2. 定出回答要点：每条一句话（不超过 30 字），**不要摘抄资料原文**，只写结论。
            3. 列出这些要点用到的资料编号。
            4. 若是【丙】，在 note 里写库里确实覆盖的相关方向。

            只输出 JSON，不要解释。""";

    /** 第一段的形状。**它是硬约束** —— 反复改措辞在这种形状下写不出来。 */
    private static final String ANALYSIS_SCHEMA = """
            {"type":"object","properties":{
              "cls":{"type":"string","enum":["甲","乙","丙"]},
              "points":{"type":"array","items":{"type":"string"}},
              "srcs":{"type":"array","items":{"type":"integer"}},
              "note":{"type":"string"}},
             "required":["cls","points"]}""";

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
    /**
     * 主题树。agent 直接问它要「知识库覆盖了什么」，不通过参数层层传 ——
     * 这是库的属性，不是某一次请求的属性。
     */
    private final com.kniv.ragkb.service.tree.TreeService tree;

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
    public record HistoryContext(List<ChatMessage> turns, String excerpt, String summary,
                                 java.util.function.Function<List<String>, String> excerptLookup) {
        public static final HistoryContext EMPTY =
                new HistoryContext(List.of(), null, null, null);

        /**
         * 历史召回**必须在规划之后**才解析，所以这里存的是一个函数而不是字符串。
         *
         * <p>为什么：规划器把「那它呢」改写成了自包含的查询（「那它的键为什么要带上
         * 模型名」），用那个去检索才召得回定义「它」的那一轮。而用用户原话当检索键的话，
         * 键就是「那它呢」这四个字 —— 笔记改写得再好也匹配不上，因为那一轮的文本里
         * 根本没有「它」。规划器已经做了这个改写，只是改写发生在历史检索**之后**，
         * 白白浪费了。
         */
        public HistoryContext withExcerpt(String ex) {
            return new HistoryContext(turns, ex, summary, excerptLookup);
        }

        /** 用一批查询去召回历史；没有 lookup 时返回 null */
        public String lookupExcerpt(List<String> queries) {
            return excerptLookup == null ? null : excerptLookup.apply(queries);
        }
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

        // 规划之后才召回历史 —— 一步的顺序是关键。
        //
        // **两个都用，取并集**：
        //   · 规划后的查询能解析指代（「那它呢」→「缓存键为什么带模型名」），
        //     用原话当键的话，键就是「那它呢」四个字，而那一轮里根本没有「它」
        //   · 但改写也会**换掉用户自己用的词**。实测：问「最开始说的那个项目代号
        //     是什么」，规划器改写后就不再提「项目代号」，于是召不回埋代号的那一轮。
        //     而用户的原话里恰好带着「项目代号」这个能命中的词。
        //
        // 只取其一都会丢掉另一半。并集的代价是多几次向量检索（每次约 50ms），
        // 换来的是两种问法都召得回。
        List<String> retrievalQueries = new ArrayList<>(queries.size() + 1);
        retrievalQueries.add(question);
        retrievalQueries.addAll(queries);
        String excerpt = hist.lookupExcerpt(retrievalQueries);
        if (excerpt != null && !excerpt.isBlank()) {
            hist = hist.withExcerpt(excerpt);
        }

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
        String answer = answer(ref, question, contexts, hist, enough, onEvent);
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
        // 经典模式没有规划这一步，只能拿原问题去召回。
        // agent 模式则在规划之后用改写过的查询 —— 那才是能解析指代的键。
        String excerpt = hist.lookupExcerpt(List.of(question));
        if (excerpt != null && !excerpt.isBlank()) {
            hist = hist.withExcerpt(excerpt);
        }
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

    /**
     * 把第一段的分析渲染成给第二段看的一段话。
     *
     * <p>措辞是**要求**而不是建议：这一段存在的全部意义就是让第二段**不必再想一遍**。
     * 但要注意它仍然是提示词层面的要求 —— 第二段走的是普通流式调用（思考照开），
     * 所以这版实验能测的是「**分析已完成，思考会不会自己变短**」；
     * 若还长，下一步才是给第二段也关掉思考。
     */
    private String renderAnalysis(JsonNode node) {
        StringBuilder sb = new StringBuilder("【已完成的分析（直接照着写，不要重复分析）】\n");
        sb.append("类别：").append(node.path("cls").asText("乙")).append('\n');
        JsonNode srcs = node.path("srcs");
        if (srcs.isArray() && !srcs.isEmpty()) {
            List<String> ids = new ArrayList<>();
            srcs.forEach(s -> ids.add("[" + s.asInt() + "]"));
            sb.append("用到的资料：").append(String.join(" ", ids)).append('\n');
        }
        sb.append("回答要点：\n");
        int i = 1;
        for (JsonNode p : node.path("points")) {
            sb.append(i++).append(". ").append(p.asText()).append('\n');
        }
        String note = node.path("note").asText("");
        if (!note.isBlank()) {
            sb.append("补充（若库里没有相关资料，用它点出确实覆盖的方向）：").append(note).append('\n');
        }
        sb.append("\n要求：**直接依据以上要点写回答**，不要再分析、不要复述资料原文、"
                + "不要反复修改措辞，也不要引用上面没列出的编号。");
        return sb.toString();
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
        return answer(ref, question, contexts, hist, true, onEvent);
    }

    /** {@code enough} = 上面的 ③ 评估判定「资料够不够」。
     *
     *  <p>**它才是"乙还是丙"的机械信号** —— 而不是 {@code contexts.isEmpty()}。
     *  2026-09-21 实测踩过：检索**几乎从不返回空**（那一轮 21 题里，
     *  连知识库根本答不了的 6 题也召回了 8~15 段），所以"有没有资料"永远为真、
     *  判出来永远是【乙】，于是模型被明确告知「资料里有内容、不要提知识库中没有」——
     *  ungrounded 那一类 **6/6 全挂**（都挂在"没标注通用知识"）。
     *  **召回到了东西，和资料能回答这个问题，是两件事。**
     */
    private String answer(ProviderRegistry.Ref ref, String question, List<ChunkHit> contexts,
                          HistoryContext hist, boolean enough, Consumer<AgentEvent> onEvent) {
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

        // ── 提示词预算 ──
        // 超限不是渐进退化而是悬崖：实测 num_ctx=10240 时 9920 token 正常，
        // 再多一点 Ollama 就把整个上下文重置到 5122 token，**丢掉开头** ——
        // 也就是系统提示词。后果是回答不再受任何约束（三段式、引用标注、
        // 「不得编造」全没了），而模型照常返回一个看起来正常的回答，不报错。
        //
        // 所以在这里按优先级裁：先丢摘要，再丢召回片段，再丢最旧的历史，
        // 最后才对资料动手 —— 资料是事实依据，丢了回答就没有根。
        String overview = tree.overview();

        int reserve = props.getAgent().getGenerationReserveTokens();
        int budget = Math.max(1024,
                props.getAgent().getPromptWindowTokens() - reserve - PromptBudget.estimateTokens(answerSystem()));
        List<String> ctxTexts = new ArrayList<>(contexts.size());
        for (ChunkHit h : contexts) {
            ctxTexts.add(h.getContent());
        }
        List<String> histTexts = new ArrayList<>();
        if (history != null) {
            for (ChatMessage m : history) {
                histTexts.add(m.content());
            }
        }
        int over = PromptBudget.estimateTokens(ctxTexts) + PromptBudget.estimateTokens(histTexts)
                + PromptBudget.estimateTokens(historyExcerpt) + PromptBudget.estimateTokens(convSummary)
                + PromptBudget.estimateTokens(overview) + PromptBudget.estimateTokens(question);
        if (over > budget) {
            // 裁的顺序（最不重要的先走）：最旧的历史 → 召回片段 → 资料。
            //
            // **召回片段排在靠后是刻意的**：它的全部意义就是补偿被裁掉的历史
            // （见 HistoryContext 的说明），把它第一个丢掉等于这个功能白做。
            // 第一版就是那么写的（摘要 → 召回片段 → 历史 → 资料），
            // 结果每次触发裁剪，召回片段都成了 0 字 —— 实测踩过。
            //
            // **摘要原先也是第一个丢的，2026-09-18 改了**：摘要是「当前值」的兜底，
            // 而召回片段可能带回**已被改掉的旧值**（片段是原文/笔记，没有"谁更新"的机制）。
            // 实测（tools/stale-recall-probe.py）：只召回旧片段而无摘要时 3/3 端出陈旧值，
            // 有摘要时 3/3 救回 —— 所以两者**共进退**：片段被丢掉时摘要才跟着丢。
            // 代价很小：变化式摘要只有 12 条 × ≤40 字 ≈ 400 token。
            //
            // 资料放最后，它是事实依据，丢了回答就没有根。

            // takeLast 而不是 take：history 是最旧在前的，要留的是末尾那几条
            int histKeep = Math.min(histTexts.size(), props.getAgent().getTrimKeepTurns() * 2);
            history = PromptBudget.takeLast(history, histKeep);

            int histTokens = PromptBudget.estimateTokens(PromptBudget.takeLast(histTexts, histKeep));
            int fixed = histTokens + PromptBudget.estimateTokens(overview)
                    + PromptBudget.estimateTokens(question);
            // 先假设保留片段，看看还剩多少给资料；实在放不下才丢片段
            if (fixed + PromptBudget.estimateTokens(historyExcerpt) > budget * 3 / 4) {
                historyExcerpt = null;
                hasExcerpt = false;
                // 摘要与召回片段共进退 —— 片段在，摘要就在（它是「当前值」的兜底）；
                // 片段走了，摘要才跟着走。顺序反过来会造出最坏组合：
                // 留着可能过时的片段、丢掉写着当前值的摘要。
                convSummary = null;
                hasSummary = false;
            }
            int usedByOthers = fixed
                    + (hasExcerpt ? PromptBudget.estimateTokens(historyExcerpt) : 0);
            int ctxBudget = Math.max(512, budget - usedByOthers);
            int keep = Math.max(1, PromptBudget.keepWithin(ctxTexts, ctxBudget));
            contexts = PromptBudget.take(contexts, keep);
            log.info("提示词超预算（估 {} > {} token），已裁剪：资料 {}→{} 段，历史 {}→{} 条，召回片段 {}",
                    over, budget, ctxTexts.size(), contexts.size(), histTexts.size(), histKeep,
                    hasExcerpt ? "保留" : "丢弃");
        }

        // 召回时排除了「一定留在窗口里的那几条」，但哪些真会留下要等预算裁完才知道，
        // 所以可能重复。这里按内容去重：重复的内容既浪费预算，又会让模型
        // 以为同一件事被强调了两遍。
        historyExcerpt = dedupeExcerpt(historyExcerpt, history);
        hasExcerpt = historyExcerpt != null && !historyExcerpt.isBlank();

        StringBuilder user = new StringBuilder();
        // 知识库覆盖范围。放在最前是因为它在「资料不足」时最有用 ——
        // 没有它，检索不到就只能回一句「知识库中没有」，而说不出
        // 「没有 X，但有 Y 和 Z 两个相关方向」。
        if (overview != null && !overview.isBlank()) {
            user.append("【知识库主题概览】（本知识库覆盖了哪些方向，供你判断该往哪找、"
                    + "以及资料不足时告知用户库里有什么）\n")
                    .append(overview).append("\n\n");
        }
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
                    .append("\n（以上是本次对话早前说过的内容。它的效力排在"
                            + "【参考资料】之后、你自己的通用知识之前："
                            + "问题若能用它回答，就依据它回答，并说明这是本次对话之前提到的；"
                            + "只有当它也回答不了时，才说知识库没有、再给通用知识。）\n\n");
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
                        .append("（第 ").append(h.getSeq()).append(" 块）");
                // **带上语境行**（开关，默认关）：思考的大头是「逐条扫描这些块」
                // （实测原文：「[1] 提到了…但没有…[2] 提到了…」，15 段扫一遍 ≈ 700 token）。
                // 把「这段能回答什么」直接给出来，它就不必自己扫 —— 而代价从
                // **decode**（75 token/秒）挪到 **prefill**（3800 token/秒），差约 50 倍。
                //
                // 措辞要注意：语境行本身是**问句**（「为什么令牌桶算法的桶容量是2？」），
                // 所以必须标明它是「本段可回答的问题」，否则 15 个问句摆在用户问题旁边
                // 会把模型带偏。
                if (props.getAgent().isCtxInPrompt() && h.getCtx() != null && !h.getCtx().isBlank()) {
                    user.append("｜本段可回答：").append(h.getCtx());
                }
                user.append('\n').append(h.getContent()).append("\n\n");
            }
        }
        user.append("【问题】\n").append(question);
        // 排查用：模型答「资料中没有」时，先分清是没检索到、还是检索到了它不用。
        // 同时打出估算的 token 数与预算占比 —— 这是唯一能看出「离悬崖还有多远」的数，
        // 不记的话只能等它静默丢系统提示词才发现。
        int usedTokens = over;
        log.debug("回答注入：资料 {} 段 / 主题概览 {} 字 / 召回片段 {} 字 / 摘要 {} 字 / 近轮 {} 条"
                        + "　→ 估算 {} token / 预算 {}（{}%）",
                contexts.size(),
                overview == null ? 0 : overview.length(),
                hasExcerpt ? historyExcerpt.length() : 0,
                hasSummary ? convSummary.length() : 0,
                history == null ? 0 : history.size(),
                usedTokens, budget + reserve + PromptBudget.estimateTokens(answerSystem()),
                Math.round(100.0 * usedTokens / Math.max(1, budget + reserve
                        + PromptBudget.estimateTokens(answerSystem()))));

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
                ref.providerId(), ref.model(), TEMPERATURE,
                // 系统提示的哈希进键 —— 改提示词（含"思考形状"这类开关）自动失效，
                // 而不是继续拿旧提示词跑出来的答案。见 CacheService.answerKey。
                CacheService.hash(answerSystem(enough) + "|" + answerPathTag()));

        CachedAnswer hit = cache.getAnswer(cacheKey);
        if (hit != null && hit.getAnswer() != null && !hit.getAnswer().isBlank()) {
            log.info("回答缓存命中，跳过生成（{} 字）", hit.getAnswer().length());
            // 缓存命中时一次性推出整段：客户端渲染是瞬时的，
            // 再逐字模拟反而增加无谓往返
            onEvent.accept(AgentEvent.answerToken(hit.getAnswer()));
            return hit.getAnswer();
        }

        List<ChatMessage> messages = new ArrayList<>();
        messages.add(ChatMessage.system(answerSystem(enough)));
        // 历史放在资料之前：事实依据仍来自资料，历史只用来理解指代
        if (history != null) {
            messages.addAll(history);
        }
        // ---- 两段式：先把「想」挤进固定 schema，再写正文 ----
        //
        // 依据见 ANALYSIS_PROMPT 的注释：思考的大头是**同一件事换措辞说十几遍**，
        // 而语法约束是唯一能截断它的东西。第一段用 `json()`（关思考 + format），
        // 它的产物由代码原样拼进第二段 —— 模型不必再"想一遍"，也不必复述资料。
        String planBlock = "";
        if (props.getAgent().isTwoStage()) {
            try {
                String reply = json(ref, ANALYSIS_PROMPT, user.toString(), TEMPERATURE, ANALYSIS_SCHEMA);
                JsonNode node = JsonExtract.parseObject(mapper, reply);
                if (node != null && node.path("points").isArray() && !node.path("points").isEmpty()) {
                    planBlock = renderAnalysis(node);
                    log.debug("两段式·第一段产出：{} 字", planBlock.length());
                } else {
                    log.warn("两段式第一段没产出要点，退回单段：{}", truncate(reply));
                }
            } catch (Exception e) {
                log.warn("两段式第一段失败，退回单段：{}", e.getMessage());
            }
            if (!planBlock.isEmpty()) {
                user.append("\n\n").append(planBlock);
            }
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
        // 接住这一轮的计时：**prefill 与 decode 分开**才谈得上"提速往哪使劲"。
        // 此前 Ollama 在末帧里给的 prompt_eval_* / eval_* 全被丢掉，
        // 于是只能量到「发出 → 首字」这个混着两种开销的总数。
        java.util.concurrent.atomic.AtomicReference<com.kniv.ragkb.provider.ChatStats> stats =
                new java.util.concurrent.atomic.AtomicReference<>(
                        com.kniv.ragkb.provider.ChatStats.none());
        providers.chatStream(ref, messages, TEMPERATURE,
                piece -> {
                    out.append(piece);
                    onEvent.accept(AgentEvent.answerToken(piece));
                },
                onThinking,
                stats::set);
        if (thinkBuf.length() > 0) {
            onEvent.accept(AgentEvent.thinking(thinkBuf.toString()));
        }
        com.kniv.ragkb.provider.ChatStats st = stats.get();
        if (st.promptTokens() > 0 || st.evalTokens() > 0) {
            // 单独一条事件，不动 done 的载荷 —— 客户端不认识就忽略
            onEvent.accept(AgentEvent.of(AgentEvent.STATS,
                    "promptTokens", st.promptTokens(),
                    "promptMs", st.promptMs(),
                    "evalTokens", st.evalTokens(),
                    "evalMs", st.evalMs(),
                    "loadMs", st.loadMs(),
                    "tokPerSec", Math.round(st.tokensPerSecond() * 10) / 10.0));
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

    /**
     * 去掉召回片段里与「已留在提示词中的历史」重复的行。
     *
     * <p>{@link com.kniv.ragkb.service.chat.HistoryIndexService} 侧只排除了
     * 「无论预算怎么裁都会留下」的那几条，所以偏旧的那部分仍可能被召回 ——
     * 那是刻意的（预算裁剪时它们就是靠这一步捞回来的），代价是正常情况下会重复。
     * 这里按内容比对去掉。
     *
     * <p>比对用整段文本而不是前缀：召回片段里每条都是完整消息（超预算时整条不取，
     * 而不是截一半），所以精确比对是可行的。
     */
    private static String dedupeExcerpt(String excerpt, List<ChatMessage> history) {
        if (excerpt == null || excerpt.isBlank() || history == null || history.isEmpty()) {
            return excerpt;
        }
        Set<String> kept = new HashSet<>();
        for (ChatMessage m : history) {
            kept.add(normaliseForCompare(m.content()));
        }
        StringBuilder out = new StringBuilder();
        for (String line : excerpt.split("\n")) {
            String body = line.replaceFirst("^(用户|助手)：", "");
            if (kept.contains(normaliseForCompare(body))) {
                continue;
            }
            out.append(line).append('\n');
        }
        String s = out.toString().strip();
        log.debug("去重：召回 {} 字 / 历史 {} 条 → 保留 {} 字",
                excerpt.length(), history.size(), s.length());
        if (s.isEmpty() && !excerpt.isBlank()) {
            String first = excerpt.split("\n")[0];
            log.debug("  ↑ 被全部去掉。召回首行=「{}」",
                    first.substring(0, Math.min(50, first.length())));
        }
        return s.isEmpty() ? null : s;
    }

    /** 比对用：抹掉空白差异，避免因换行/空格不同而漏判 */
    private static String normaliseForCompare(String s) {
        return s == null ? "" : s.replaceAll("\\s+", " ").strip();
    }

    private static String truncate(String s) {
        return clip(s, 200);
    }
}
