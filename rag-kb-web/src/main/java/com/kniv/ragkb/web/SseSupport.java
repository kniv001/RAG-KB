package com.kniv.ragkb.web;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.provider.ProviderException;
import com.kniv.ragkb.security.crypto.HybridCryptoService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;

/**
 * SSE + 逐事件加密的公共部分。
 *
 * <p>为什么流式要单独处理加密：过滤器的做法是「把整个响应缓存下来再加密」，
 * 而 SSE 的价值恰恰是边生成边推 —— 缓存会彻底破坏流式。所以流式路径只解密请求，
 * 响应改由这里逐事件加密，密钥由过滤器经请求属性交接过来。
 *
 * <p>超时设为 0（不超时）：agent 模式一次问答可能跑好几轮，每轮本地模型都要几十秒，
 * 固定超时会把长回答掐断。对端卡死由 provider 层的 streamIdleSeconds 兜底。
 */
@Component
@RequiredArgsConstructor
public class SseSupport {

    private final HybridCryptoService crypto;
    private final ObjectMapper objectMapper;

    public SseEmitter open() {
        return new SseEmitter(0L);
    }

    /** 发送一个事件；key 为 null 时按明文发（crypto.enabled=false 的调试场景）。 */
    public void send(SseEmitter emitter, byte[] aesKey, String event, Object data) {
        try {
            Object payload = data;
            if (aesKey != null) {
                payload = crypto.encryptBody(aesKey, objectMapper.writeValueAsString(data));
            }
            emitter.send(SseEmitter.event().name(event).data(payload, MediaType.APPLICATION_JSON));
        } catch (IOException e) {
            throw new ProviderException("sse", "客户端已断开");
        }
    }

    /** 尽量把错误推给前端；推不出去（连接已断）就静默放弃。 */
    public void sendErrorQuietly(SseEmitter emitter, byte[] aesKey, Exception e) {
        try {
            send(emitter, aesKey, "error",
                    java.util.Map.of("message", String.valueOf(e.getMessage())));
        } catch (Exception ignored) {
            // 已经断了，只能放弃
        }
    }
}
