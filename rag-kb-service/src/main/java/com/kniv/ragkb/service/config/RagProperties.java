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

        /** 每轮规划的查询数上限 */
        private int queriesPerRound = 3;

        /** 单轮检索返回给评估阶段的段数 */
        private int topKPerQuery = 4;

        /** 累积上下文的上限段数，防止多轮累积把上下文撑爆 */
        private int maxContexts = 12;
    }
}
