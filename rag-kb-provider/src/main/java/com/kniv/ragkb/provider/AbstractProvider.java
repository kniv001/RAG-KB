package com.kniv.ragkb.provider;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.kniv.ragkb.provider.model.ChatMessage;
import lombok.extern.slf4j.Slf4j;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.function.Consumer;
import java.util.stream.Stream;

/**
 * 提供方共用骨架：HTTP 管线、JSON 组装、错误归一。
 *
 * <p>用 JDK 自带的 {@link HttpClient} 而不是 RestClient/WebClient：
 * 它的 {@code BodyHandlers.ofLines()} 原生支持逐行读流，做 NDJSON 与 SSE 都是零依赖，
 * 而且不必为流式再引入一整套响应式栈。
 */
@Slf4j
abstract class AbstractProvider implements ModelProvider {

    protected final String id;
    protected final ProviderProperties.Entry cfg;
    protected final ObjectMapper mapper;
    protected final HttpClient http;
    private final Duration timeout;
    private final Duration streamIdle;

    protected AbstractProvider(String id, ProviderProperties.Entry cfg, ObjectMapper mapper,
                               Duration timeout, Duration streamIdle) {
        this.id = id;
        this.cfg = cfg;
        this.mapper = mapper;
        this.timeout = timeout;
        this.streamIdle = streamIdle;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .followRedirects(HttpClient.Redirect.NORMAL)
                .build();
    }

    @Override
    public String id() {
        return id;
    }

    @Override
    public String kind() {
        return cfg.getKind();
    }

    // ---------------- 请求构造 ----------------

    protected HttpRequest.Builder base(String path) {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(join(cfg.getBase(), path)))
                .header("Content-Type", "application/json")
                .timeout(timeout);
        String key = cfg.getApiKey();
        if (key != null && !key.isBlank()) {
            b.header("Authorization", "Bearer " + key.trim());
        }
        return b;
    }

    protected static String join(String base, String path) {
        String b = base == null ? "" : base.trim();
        if (b.endsWith("/")) {
            b = b.substring(0, b.length() - 1);
        }
        return b + path;
    }

    protected ArrayNode messagesArray(List<ChatMessage> messages) {
        ArrayNode arr = mapper.createArrayNode();
        for (ChatMessage m : messages) {
            ObjectNode n = arr.addObject();
            n.put("role", m.role());
            n.put("content", m.content());
        }
        return arr;
    }

    // ---------------- 请求执行 ----------------

    protected JsonNode postJson(String path, JsonNode body) {
        HttpRequest req = base(path)
                .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> resp = send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return parse(resp);
    }

    protected JsonNode getJson(String path) {
        HttpRequest req = base(path).GET().build();
        HttpResponse<String> resp = send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return parse(resp);
    }

    /**
     * 逐行读流式响应。
     *
     * <p>SSE 与 NDJSON 的区别只在于每行要不要剥掉 {@code data: } 前缀，所以两者
     * 共用这一条管线，由各自子类在回调里处理行内容。
     */
    protected void postStream(String path, JsonNode body, Consumer<String> onLine) {
        HttpRequest req = base(path)
                .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .timeout(streamIdle)
                .build();
        try {
            HttpResponse<Stream<String>> resp = http.send(req, HttpResponse.BodyHandlers.ofLines());
            if (resp.statusCode() / 100 != 2) {
                String detail = resp.body().limit(20).reduce("", (a, b) -> a + b + "\n");
                throw new ProviderException(id, "HTTP " + resp.statusCode() + "：" + truncate(detail));
            }
            try (Stream<String> lines = resp.body()) {
                lines.forEach(onLine);
            }
        } catch (IOException e) {
            throw new ProviderException(id, "流式请求失败：" + e.getMessage(), e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ProviderException(id, "流式请求被中断", e);
        }
    }

    private <T> HttpResponse<T> send(HttpRequest req, HttpResponse.BodyHandler<T> handler) {
        try {
            return http.send(req, handler);
        } catch (IOException e) {
            throw new ProviderException(id,
                    "无法连接 " + cfg.getBase() + "：" + e.getMessage(), e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ProviderException(id, "请求被中断", e);
        }
    }

    private JsonNode parse(HttpResponse<String> resp) {
        String text = resp.body() == null ? "" : resp.body();
        if (resp.statusCode() / 100 != 2) {
            throw new ProviderException(id, "HTTP " + resp.statusCode() + "：" + truncate(text));
        }
        try {
            return mapper.readTree(text);
        } catch (IOException e) {
            throw new ProviderException(id, "响应不是合法 JSON：" + truncate(text), e);
        }
    }

    protected static String truncate(String s) {
        if (s == null) {
            return "";
        }
        String t = s.strip();
        return t.length() <= 300 ? t : t.substring(0, 300) + "…";
    }

    @Override
    public String toString() {
        return kind() + ":" + id + "@" + cfg.getBase();
    }
}
