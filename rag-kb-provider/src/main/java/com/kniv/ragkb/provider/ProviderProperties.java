package com.kniv.ragkb.provider;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 模型提供方配置。
 *
 * <p>本地与云端走同一套接口，运行时可切。加一家新服务只是加一段配置，
 * 只要它是 OpenAI 兼容端点（DeepSeek / 通义 / Moonshot / 智谱 / 硅基流动 /
 * OpenAI / vLLM / LM Studio 都是），不用改代码。
 *
 * <p>写法：{@code providerId/modelName}，例如 {@code local/qwen3:8b}、
 * {@code deepseek/deepseek-chat}。用斜杠是因为模型名本身常含冒号（qwen3:8b），
 * 用冒号分隔会歧义。
 */
@Data
@ConfigurationProperties(prefix = "ragkb.provider")
public class ProviderProperties {

    /** 默认对话模型，格式 providerId/model */
    private String defaultChat = "local/qwen3:8b";

    /** 默认向量模型 */
    private String defaultEmbed = "local/bge-m3";

    /** 连接与读取超时（秒）。本地大模型推理慢，默认给足。 */
    private long timeoutSeconds = 300;

    /** 流式响应允许的最长静默时间（秒），超时即判定对端卡死 */
    private long streamIdleSeconds = 120;

    private Map<String, Entry> providers = new LinkedHashMap<>();

    @Data
    public static class Entry {
        /** ollama 或 openai */
        private String kind = "ollama";

        /** 显示名 */
        private String label;

        /** 基址。ollama 形如 http://127.0.0.1:11434；openai 形如 https://api.deepseek.com/v1 */
        private String base;

        /** 仅 kind=openai 使用 */
        private String apiKey = "";

        private List<String> chatModels = List.of();

        private List<String> embedModels = List.of();

        public boolean isOpenAi() {
            return "openai".equalsIgnoreCase(kind);
        }

        public String displayLabel() {
            return label == null || label.isBlank() ? kind : label;
        }
    }
}
