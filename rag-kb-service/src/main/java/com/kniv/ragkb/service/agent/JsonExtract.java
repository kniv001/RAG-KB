package com.kniv.ragkb.service.agent;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.ArrayList;
import java.util.List;

/**
 * 从模型输出里抠出 JSON。
 *
 * <p>小模型几乎不会乖乖只输出 JSON —— 常见的有三种干扰：
 * 用 ```json 代码块包起来、前后加一句「好的，我来分析」、或者思考过程写在前面。
 * 所以不能直接 {@code readTree(整段)}，必须先把第一个完整的平衡大括号片段找出来。
 */
public final class JsonExtract {

    private JsonExtract() {
    }

    /** 找第一段平衡的 {...}，忽略字符串里的括号。找不到返回 null。 */
    public static String firstObject(String text) {
        if (text == null) {
            return null;
        }
        int start = text.indexOf('{');
        while (start >= 0) {
            int depth = 0;
            boolean inString = false;
            boolean escaped = false;
            for (int i = start; i < text.length(); i++) {
                char ch = text.charAt(i);
                if (escaped) {
                    escaped = false;
                    continue;
                }
                if (ch == '\\') {
                    escaped = true;
                    continue;
                }
                if (ch == '"') {
                    inString = !inString;
                    continue;
                }
                if (inString) {
                    continue;
                }
                if (ch == '{') {
                    depth++;
                } else if (ch == '}') {
                    depth--;
                    if (depth == 0) {
                        return text.substring(start, i + 1);
                    }
                }
            }
            start = text.indexOf('{', start + 1);
        }
        return null;
    }

    /** 解析成 JsonNode；失败返回 null（调用方决定降级策略，而不是抛异常中断整轮）。 */
    public static JsonNode parseObject(ObjectMapper mapper, String text) {
        String json = firstObject(text);
        if (json == null) {
            return null;
        }
        try {
            return mapper.readTree(json);
        } catch (Exception e) {
            return null;
        }
    }

    /** 取字符串数组字段；缺失或类型不符时返回空列表。 */
    public static List<String> stringArray(JsonNode node, String field, int limit) {
        List<String> out = new ArrayList<>();
        if (node == null) {
            return out;
        }
        JsonNode arr = node.get(field);
        if (arr == null || !arr.isArray()) {
            return out;
        }
        for (JsonNode item : arr) {
            String s = item.asText("").strip();
            if (!s.isEmpty()) {
                out.add(s);
            }
            if (out.size() >= limit) {
                break;
            }
        }
        return out;
    }

    public static String string(JsonNode node, String field, String fallback) {
        if (node == null) {
            return fallback;
        }
        JsonNode v = node.get(field);
        return v == null || v.isNull() ? fallback : v.asText(fallback);
    }

    public static boolean bool(JsonNode node, String field, boolean fallback) {
        if (node == null) {
            return fallback;
        }
        JsonNode v = node.get(field);
        return v == null || v.isNull() ? fallback : v.asBoolean(fallback);
    }
}
