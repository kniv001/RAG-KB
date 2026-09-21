package com.kniv.ragkb.service.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * RAG 管线参数。与 Python 版的默认值保持一致 —— 两套系统共用一个库，
 * 检索口径不同会导致同一篇文档在两边召回结果对不上。
 */
@Data
@ConfigurationProperties(prefix = "ragkb.rag")
@Component
public class RagProperties {

    /** 最终返回给模型的上下文段数 */
    private int topK = 6;

    /** 余弦距离上限，超过视为不相关 */
    private double maxDistance = 0.60;

    /** 每路召回的候选池大小，融合后再截断到 topK。池子太小会让 RRF 失去意义 */
    private int rerankPool = 30;

    /** 默认检索模式：vector / keyword / hybrid */
    private String defaultMode = "hybrid";

    // 刻意不在这里再放 embedModel / chatModel ——
    // 模型的唯一来源是 ragkb.provider.default-embed / default-chat。
    // 曾经两处都配，改了 A 忘了改 B 就会出现「配置明明改了、跑的却是旧模型」，
    // 而且从日志上完全看不出来。

    private Agent agent = new Agent();

    private History history = new History();

    private Tree tree = new Tree();

    private TurnDoc turnDoc = new TurnDoc();

    /**
     * 轮次笔记：把一轮对话改写成自包含的一段话，索引的是它而不是原文。
     *
     * <p>解决的是「单条 message 不是好的检索单元」—— 用户那句常带指代、
     * 助手那句常脱离问题、两边都夹着包装话术。改写之后才是能独立检索的单元。
     */
    @Data
    public static class TurnDoc {

        private boolean enabled = true;

        /**
         * 只改写窗口之外的轮次。
         *
         * <p>必须与 {@code ChatService.HISTORY_LIMIT} 和 {@code Summary.windowMessages}
         * 取同一个值：这样一条消息要么"在窗口里（原文）"、要么"在窗口外（笔记）"，
         * 不会两头都出现、让模型看到同一件事的两种说法。
         */
        private int windowMessages = 16;

        /** 一次后台任务改写几轮。攒批是为了少进几次模型 —— 每次进模型都要占推理槽 */
        private int batchMessages = 4;
    }

    /**
     * 主题树：把全部块聚成若干主题簇，每簇一句概括。
     *
     * <p>解决的是「语料一大，扁平的 top-k 就看不出全局」—— 检索不到时只能回
     * 「知识库中没有」，而说不出「没有 X，但有 Y 和 Z 两个相关方向」。
     */
    @Data
    public static class Tree {

        private boolean enabled = true;

        /** 簇数上限。语料很小时实际簇数由 √(n/2) 决定，这里只是封顶 */
        private int maxClusters = 12;

        /** 给每个簇挑几条代表片段去概括。太多没必要，概括不需要读全文 */
        private int samplesPerCluster = 5;

        /**
         * 检索时把搜索范围收窄到最相关的几个主题簇。
         *
         * <p>0 表示不收窄（退回全库检索）。语料小的时候收窄反而有害 ——
         * 万一聚类把相关的块分到了别的簇，收窄就是把正确答案挡在门外。
         * 语料大了（几百块以上）才值得开。
         */
        private int narrowToClusters = 0;

        /**
         * 是否把主题概览注入提示词。
         *
         * <p>开着的好处：回答能说出知识库覆盖了哪些方向，而不只是「没有」。
         * 代价是每轮多占一些上下文 —— 但它是**固定前缀**，
         * 实测前缀复用能把重复部分的 prefill 从 819ms 降到 44ms。
         */
        private boolean injectOverview = true;

        /** 概览的字符上限，防止主题太多把上下文挤爆 */
        private int overviewChars = 700;
    }

    private Summary summary = new Summary();

    /**
     * 语境行：给每块生成一句「这段能回答什么问题」，**只进检索索引、不进提示词**。
     * 见 {@code ChunkContextService} —— 难题集实测 13/14 → 14/14，代价是入库时每块一次调用。
     */
    private Context context = new Context();

    /**
     * 会话滚动摘要 —— 历史索引漏召时的兜底。
     *
     * <p>它与历史索引是互补而非替代：索引给细节但会漏，摘要给全局但很粗。
     * 漏召时至少还剩一份覆盖全部的背景，不至于彻底断片。
     */
    @Data
    public static class Context {

        private boolean enabled = true;
    }

    @Data
    public static class Summary {

        private boolean enabled = true;

        /**
         * 最近多少条消息算「窗口内」，不进摘要。
         *
         * <p>取 16 与 {@code ChatService.HISTORY_LIMIT} 一致 —— 窗口里的原文
         * 本来就要给模型看，提前摘进摘要里是重复的。
         */
        private int windowMessages = 16;

        /**
         * 攒够多少条「掉出窗口」的消息才更新一次摘要。
         *
         * <p>每轮都算的话，一轮一次模型调用，长对话里全花在这上面。
         * 取 6（三轮）是成本与新鲜度的折中。
         */
        private int batchMessages = 6;
    }

    /**
     * 会话历史索引 —— 超出最近窗口的旧轮次不再整段丢弃，而是向量化后按需召回。
     *
     * <p>注意它是<b>增量</b>：窗口内的近轮次仍是原文全文，不受检索质量影响。
     * 只有超出窗口的部分才依赖召回，所以最坏情况是「退回到改造前的行为」，
     * 而不是「丢掉了本来有的东西」。
     */
    @Data
    public static class History {

        private boolean enabled = true;

        /** 每次召回多少条旧消息（邻接的上下文会额外带上，不计入这个数） */
        private int topK = 4;

        /**
         * 注入提示词的历史片段字符上限。
         *
         * <p>窗口总共 10240 token，其中知识库资料最多可占 12 块 × 600 字 ≈ 5400 token。
         * 1200 字约合 900 token，是能在不挤掉资料的前提下给出有效召回的量。
         */
        private int excerptChars = 1200;

        /** 单次补索引的消息条数上限：老会话首次触发时不能一次全量向量化把请求卡住 */
        private int indexBatch = 64;
    }

    /** Agentic RAG 参数 */
    @Data
    public static class Agent {

        /** 默认是否走 agent 模式；关闭则退化为经典单轮 RAG */
        private boolean enabled = true;

        /**
         * 最大检索轮次。实测 3 轮会把耗时推到 160 秒以上，而多召回的收益很小 ——
         * 降到 2 轮。真需要更多轮时用户可以显式指定。
         */
        private int maxRounds = 2;

        /**
         * 规划与评估用的小模型，形如 local/qwen3:1.7b。留空则用主模型。
         *
         * <p>这两步只需要「输出一段 JSON」，不需要 9B 级别的语言能力，
         * 却占了 agent 总耗时的七成以上（各 20-25 秒）。换小模型是收益最大的一处优化。
         *
         * <p>注意：本机是 8GB 显存，装不下「8B 对话模型 + 向量模型」，
         * Ollama 会在两者间反复换入换出（每次 5-10 秒）。所以这里选的模型
         * 还必须小到能和向量模型同时驻留，否则省下的时间又被重载吃掉。
         */
        private String utilityModel = "";

        /**
         * 规划与评估是否强制结构化输出（关闭模型思考 + 语法约束 JSON）。
         *
         * <p>实测（本机 8GB 显卡，qwen3:4b @ 8192）：
         * <table>
         *   <tr><th></th><th>plan</th><th>assess</th></tr>
         *   <tr><td>关闭</td><td>51.5 秒</td><td>31.4 秒</td></tr>
         *   <tr><td>开启</td><td>0.94 秒</td><td>1.6 秒</td></tr>
         * </table>
         *
         * <p>根因：模型为吐一个三行 JSON，先生成了 4561 个 token 的推理。
         * 提示词里写「只输出 JSON」拦不住它 —— 那只是请求，这里是语法层面的强制。
         *
         * <p>留开关是因为它是「用一点点思考深度换 30~55 倍速度」的交易。
         * 若某天发现规划质量确实变差，可以关掉对照；实测四个典型问题上，
         * 关思考后拆解粒度反而更干净（见 tools/agent-plan-verify.mjs）。
         */
        private boolean structuredOutput = true;

        /**
         * 是否把模型的思考过程流式推给前端（{@code thinking} 事件）。
         *
         * <p>回答路径的瓶颈是模型在正文前先写几千字推理 —— 实测首个正文帧要等
         * <b>12~46 秒</b>，而首个<b>思考</b>帧只要 0.2~2.5 秒。推出去，等待才有内容可看。
         *
         * <p>总耗时不变，这是把「黑屏」换成「可见的进度」。注意 {@code think:false}
         * 对回答路径是净亏（推理会转进正文污染输出），所以只能这样处理。
         *
         * <p>前端应当把 thinking 事件折叠展示，绝不能混进正文 —— 它是过程不是结论。
         */
        private boolean streamThinking = true;

        /**
         * **给思考一个会终止的形状**（条目化 + 终止标记）。
         *
         * <p>为什么需要：实测 decode 占一次问答的绝大部分，而解出来看，
         * **decode 里 79~87% 的 token 是「思考」**（样本：正文 322 字 vs 思考 2093 字）。
         * 思考走独立通道、前端折叠展示，用户不看 —— 但**它和正文在同一条 decode 流里串行产生**，
         * 想多久就多等多久。而回答路径的 {@code think} **不能关**（{@code think:false}
         * 是净亏，推理会转进正文污染输出，实测过）。
         *
         * <p>所以问题不是"开不开"，而是"能不能想短点"。本开关加上一段**形状约束**：
         * 思考只允许写成有限条数的清单，写完一个终止标记就必须开始写正文。
         * 依据是这条线上已经成立过两次的规律 —— **形状本身就是约束**
         * （摘要的「变化式」、plan/assess 的结构化输出都验证过；祈使句反而弱）。
         *
         * <p>默认 <b>false</b>：还没量过它对**答案质量**的影响，而砍思考换速度
         * 必须有答案侧的尺子兜底（`tools/eval.py`），不能只跑 `latency-probe`。
         */
        private boolean shapeThinking = false;

        /**
         * **把语境行随资料一起给模型**（默认关）。
         *
         * <p>语境行（每块一句「这段能回答什么问题」）此前**只进检索索引、不进提示词** ——
         * 2026-09-18 的决定，理由是"查询侧零开销"。而 2026-09-20 用 `latency-probe`
         * 拆开计时后看到：decode 里 84~86% 的 token 是思考，思考的大头是
         * **逐条扫描全部召回块**（实测原文：「[1] 提到了…但没有…」× 15 段 ≈ 700 token）。
         *
         * <p>给出来就不必自己扫，而代价从 **decode**（~75 token/秒）挪到
         * **prefill**（~3800 token/秒）——**差约 50 倍**。
         *
         * <p>默认关：还没量过它对质量的影响，而"改模型看到的东西"必须两把尺子一起量
         * （`tools/eval.py` 的质量 + `tools/latency-probe.mjs` 的速度）。
         */
        private boolean ctxInPrompt = false;

        /**
         * **两段式回答**：先把"想"挤进固定 schema（语法约束），再写正文。
         *
         * <p>为什么：直接读思考原文看到，decode 的大头是**同一件事换措辞说十几遍**
         * （最极端那题：思考 3598 字 / 正文 58 字 = 62 倍，里面是同一句定义的反复微调）。
         * 逐字指标测不到它（每遍用词都不同 ⇒ shingle 重合只有 2~19%），
         * 形状约束也拦不住（"最多 4 条"只压了 14%）。
         *
         * <p>**唯一能截断这个循环的是语法约束** —— {@code format} 下模型只能往固定格子里填。
         * 这正是 plan/assess 验证过的机制（51.5s → 0.94s）。
         *
         * <p>默认 <b>false</b>：多一次模型调用，而且第二段仍走普通流式（思考照开），
         * 所以它先测的是「分析已完成，思考会不会自己变短」。质量与速度都要量。
         */
        private boolean twoStage = false;

        /**
         * **类型判定改由代码给**（默认关）：系统提示里不再让模型"先判断属于哪一类"。
         *
         * <p>依据（2026-09-21 实测）：把思考按句切开，标出与**输出契约**有关的句子
         * （类别判定 / 要不要声明缺失 / 要不要标注通用知识 / 引用格式 / 文风），
         * 占全部思考的 **20%**，而 chitchat 题（「你是谁？」）高达 **49%** ——
         * **越不需要想内容的题，契约推理占比越高**。
         *
         * <p>契约就是 {@code ANSWER_SYSTEM} 里那张长表，模型**每道题都要重推一遍**。
         * 而判定本身是机械的：**有没有资料**决定【乙】还是【丙】。
         * 而"模型做不稳的机械活交给代码"这条已经印证过三次
         * （覆盖边 / 数字判据 / 只增不删）。
         *
         * <p>【甲】不在此列 —— 应用**没有**闲聊检测（规划器一律出查询、一律检索），
         * 所以代码判不了甲，只能留一句极短的例外。这是本开关的已知边界。
         *
         * <p><b>2026-09-21 实测：这条路死了，默认保持 false。</b>三层都试过，逐层否掉：
         *
         * <p><b>① 代码没有这个信号。</b>先后用过两个，都不是"不准"，而是<b>量的不是同一件事</b>：
         * <ul>
         *   <li>{@code contexts.isEmpty()} —— 检索<b>从不返回空</b>：21 题里连知识库
         *       根本答不了的 6 题也召回了 8~15 段。于是判定恒为【乙】。</li>
         *   <li>③ 评估的 {@code enough} —— <b>恒为 true</b>。它的口径是按"要不要再来一轮检索"
         *       调的（明确禁止几种判"不够"的理由），而 {@code 乙/丙} 问的是
         *       "知识库覆不覆盖这个主题"。实测理由还常与判定自相矛盾
         *       （原文：「资料中没有任何一条与 Prometheus 相关」⇒ {@code enough=true}）。</li>
         * </ul>
         * <b>召回到了东西，和资料能回答这个问题，是两件事。</b>
         *
         * <p><b>② 判错时代价是全面的。</b>用错误判定跑双臂：ungrounded <b>6/6 全挂</b>
         * （模型顺从地按"资料里有内容"作答，一句声明都不给）。总合格率 18/21 → 10/21。
         * <b>模型对"已由系统判定"的顺从度很高 ⇒ 这个开关只在一个信号可信时才安全。</b>
         *
         * <p><b>③ 换成"一次便宜的专用调用"也不行。</b>单独问模型只要类别
         * （{@code tools/eval/category-probe.py}，还明写了"提到相关话题≠能回答这个问题"）
         * ⇒ 准确率 <b>16/21 = 76%</b>，而且<b>错的方向完全一致</b>：把答不了的问题判成【乙】。
         *
         * <p>⇒ <b>结论：那 20% 的契约推理不是可省的开销，它是模型在做一次真正困难的区分，
         * 而难的正是"资料提到了这个主题、却没给出问题所要的内容"—— 正是
         * {@code grounded-partial} 那一档。</b>基线（思考里有完整契约表 + 有推演余地）
         * 判得对，剥成光秃秃的分类器反而判不对。
         */
        private boolean contractInCode = false;

        /**
         * 检索不到任何资料时，是否仍让模型作答。
         *
         * <p>默认 <b>true</b>：知识库没覆盖的问题，先说明知识库没有、再用通用知识回答，
         * 并明确标注哪部分不是来自用户资料（见 {@code ANSWER_SYSTEM} 的情况【二】）。
         *
         * <p>关掉则退回快速兜底 —— 不调模型，直接返回一句「资料中没有相关内容」。
         * 秒回，但那句话对用户毫无帮助，而且永远是同一句。
         * 保留这个开关是因为它为每个「知识库答不上来」的问题省掉一次完整生成
         * （本机含思考约 10~25 秒）。
         */
        private boolean answerWithoutContext = true;

        /**
         * 模型窗口大小。必须与 {@code ragkb.provider.providers.local.num-ctx} 一致 ——
         * 前者决定模型能装多少，后者决定我们打算往里塞多少，对不上就会触发
         * 下面那条悬崖。两处都配是刻意的冗余：写死一处的话，改了那边这边不知道。
         */
        private int promptWindowTokens = 10240;

        /**
         * 为生成预留的 token 数（思考 + 正文）。
         *
         * <p>实测 qwen3:4b 的思考在 1700~5500 字之间（约 1000~3400 token），
         * 正文 300~800 token。取 4096 是留了余量。
         */
        private int generationReserveTokens = 4096;

        /**
         * 超预算裁剪时，最近历史保留几轮。
         *
         * <p>不留不行：没有最近几轮，「那它呢」这类追问解析不了指代；
         * 留太多又会把预算吃光。4 轮是折中，更早的轮次本来就有历史索引兜底。
         */
        private int trimKeepTurns = 4;

        /** 每轮规划的查询数上限 */
        private int queriesPerRound = 3;

        /** 单轮检索返回给评估阶段的段数 */
        private int topKPerQuery = 4;

        /** 累积上下文的上限段数，防止多轮累积把上下文撑爆 */
        private int maxContexts = 12;
    }
}
