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
     * 流式对话：每收到一个增量片段回调一次 onToken。
     *
     * <p>为什么不返回 Stream：回调能保证「读完才返回」，调用方不用管资源释放；
     * 而 SseEmitter 的写入本来就是副作用的、顺序敏感的，回调语义更贴合。
     *
     * @param onToken 收到增量文本时调用（可能为空串，表示保活帧）
     */
    void chatStream(String model, List<ChatMessage> messages, double temperature,
                    Consumer<String> onToken);

    /** 批量向量化。返回顺序与入参一致。 */
    List<float[]> embed(String model, List<String> texts);

    /** 列出该提供方当前真实可用的模型名。 */
    List<String> listModels();
}
