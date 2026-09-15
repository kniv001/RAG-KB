package com.kniv.ragkb.provider;

import com.kniv.ragkb.provider.model.ChatMessage;

import java.util.List;
import java.util.function.Consumer;

/**
 * 模型提供方抽象。上层（RAG 管线、对话）只依赖这个接口，不关心背后是 Ollama 还是哪家云服务。
 *
 * <p>流式用回调而不是返回 Stream：调用方通常是 Spring MVC 的 SseEmitter，
 * 它本身就是「来一段推一段」的模型，回调式对接最直接，也避免调用方忘记关流。
 */
public interface ModelProvider {

    String id();

    String kind();

    /** 同步对话。 */
    String chat(String model, List<ChatMessage> messages, double temperature);

    /**
     * 只要一段 JSON 的同步对话 —— 规划与评估这种「输出即数据、不需要文采」的步骤走这条路。
     *
     * <p><b>为什么值得单开一个方法</b>：让模型「输出 JSON」和让模型「思考后输出 JSON」
     * 是两个数量级的开销。实测本机 qwen3:4b 为吐一个三行 JSON 先生成了 4561 个 token
     * 的推理，plan 阶段耗时 51.5 秒；改用语法约束后 0.94 秒。
     * 提示词里写「只输出 JSON」是<b>请求</b>，这里的约束是<b>强制</b>。
     *
     * <p>默认实现退化为 {@link #chat}：没有结构化输出能力的提供方仍然可用，
     * 只是拿不到那份提速。
     *
     * @param jsonSchema 期望的 JSON Schema；空则只要求「是合法 JSON」
     */
    default String chatJson(String model, List<ChatMessage> messages, double temperature,
                            String jsonSchema) {
        return chat(model, messages, temperature);
    }

    /**
     * 流式对话：每收到一个增量片段回调一次 onToken。
     *
     * <p>为什么不返回 Stream：回调能保证「读完才返回」，调用方不用管资源释放；
     * 而 SseEmitter 的写入本来就是副作用的、顺序敏感的，回调语义更贴合。
     *
     * @param onToken 收到增量文本时调用（可能为空串，表示保活帧）
     */
    void chatStream(String model, List<ChatMessage> messages, double temperature,
                    Consumer<String> onToken);

    /**
     * 流式对话，并把模型的<b>思考过程</b>一并推出来。
     *
     * <p><b>为什么需要它</b>：qwen3 这类混合推理模型在正文之前会先写几千字的推理，
     * 而推理走的是 {@code thinking} 通道，只看 {@code content} 的话这段被整个丢掉 ——
     * 实测用户要对着黑屏等 <b>12~46 秒</b>（首个 content 帧的时刻），
     * 而<b>首个 thinking 帧只要 0.2~2.5 秒</b>。推出去，等待才有内容可看。
     *
     * <p>总耗时不变 —— 这是把「黑屏」换成「可见的进度」，不是提速。
     * 注意 {@code think:false} 对回答路径是净亏（推理会转进正文污染输出，实测过），
     * 所以只能这样处理。
     *
     * <p>默认实现丢弃思考：没有推理通道的提供方行为不变。
     */
    default void chatStream(String model, List<ChatMessage> messages, double temperature,
                            Consumer<String> onToken, Consumer<String> onThinking) {
        chatStream(model, messages, temperature, onToken);
    }

    /** 批量向量化。返回顺序与入参一致。 */
    List<float[]> embed(String model, List<String> texts);

    /** 列出该提供方当前真实可用的模型名。 */
    List<String> listModels();
}
