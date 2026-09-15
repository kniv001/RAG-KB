package com.kniv.ragkb.provider.model;

/**
 * 一条对话消息。role 取 system / user / assistant。
 * 用 record 而不是 Lombok 类：它就是个不可变值对象，没有行为。
 */
public record ChatMessage(String role, String content) {

    public static ChatMessage system(String content) {
        return new ChatMessage("system", content);
    }

    public static ChatMessage user(String content) {
        return new ChatMessage("user", content);
    }

    public static ChatMessage assistant(String content) {
        return new ChatMessage("assistant", content);
    }
}
