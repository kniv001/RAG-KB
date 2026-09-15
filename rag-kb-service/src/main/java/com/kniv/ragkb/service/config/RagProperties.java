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

        /** 每轮规划的查询数上限 */
        private int queriesPerRound = 3;

        /** 单轮检索返回给评估阶段的段数 */
        private int topKPerQuery = 4;

        /** 累积上下文的上限段数，防止多轮累积把上下文撑爆 */
        private int maxContexts = 12;
    }
}
