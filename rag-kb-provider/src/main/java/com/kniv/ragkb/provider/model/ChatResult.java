package com.kniv.ragkb.provider.model;

/**
 * 一次生成的产物。
 *
 * <p>带上 providerId 与 model 是为了<b>可追溯</b>：每条助手消息都要记录
 * 是哪家、哪个模型生成的，否则事后无法解释「为什么同一个问题两次回答风格不同」。
 */
public record ChatResult(String content, String providerId, String model, boolean cached) {

    public static ChatResult of(String content, String providerId, String model) {
        return new ChatResult(content, providerId, model, false);
    }
}
