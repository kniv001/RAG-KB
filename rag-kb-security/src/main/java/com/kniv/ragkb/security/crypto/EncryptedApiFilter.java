package com.kniv.ragkb.security.crypto;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.util.AntPathMatcher;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.util.ContentCachingResponseWrapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;

/**
 * 加解密过滤器：解开请求 → 让业务处理 → 加密响应。
 *
 * <p>必须跑在 Spring Security 之前（order 很小），因为它负责把密文里的令牌
 * 注入成 Authorization 头，安全链才能读到。
 *
 * <p>请求携带三样东西：
 * <pre>
 *   X-Enc-Key    RSA-OAEP 包裹的 AES-256 密钥（每次请求由客户端新生成）
 *   X-Enc-Meta   base64({"iv","d"})，明文 {"ts":毫秒,"nonce":"..","token":".."}
 *   请求体       {"iv","d"}，明文即原始请求 JSON（仅在方法有体时）
 * </pre>
 *
 * <p>响应复用同一把 AES 密钥、<b>换新 IV</b>，体为 {"iv","d"}。
 *
 * <p>错误一律明文返回：此时尚未建立可信密钥，加密也无从谈起（客户端按普通 JSON 解析即可）。
 */
@Slf4j
@RequiredArgsConstructor
public class EncryptedApiFilter extends OncePerRequestFilter {

    private final CryptoProperties props;
    private final HybridCryptoService crypto;
    private final ObjectMapper mapper;
    private final StringRedisTemplate redis;
    private final AntPathMatcher matcher = new AntPathMatcher();

    private static final String NONCE_PREFIX = "enc:nonce:";

    /**
     * 请求属性名：解开后的 AES 密钥。
     *
     * <p>仅对 {@link CryptoProperties#getStreamPathPatterns() 流式路径}设置 ——
     * 那些路径的响应由控制器逐事件加密，需要拿到这把密钥。控制器用完后必须清零。
     */
    public static final String ATTR_AES_KEY = "ragkb.crypto.aesKey";

    /** 取流式端点交接过来的 AES 密钥；非流式请求返回 null。 */
    public static byte[] aesKeyOf(HttpServletRequest request) {
        Object v = request.getAttribute(ATTR_AES_KEY);
        return v instanceof byte[] k ? k : null;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        if (!props.isEnabled()) {
            return true;
        }
        String path = request.getRequestURI();
        for (String p : props.getExcludePaths()) {
            if (matcher.match(p, path)) {
                return true;
            }
        }
        return false;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {

        // 文件上传不套用体加密：multipart 需要单独的分片方案，见 README「已知缺口」
        String ct = request.getContentType();
        if (ct != null && ct.toLowerCase().startsWith("multipart/")) {
            chain.doFilter(request, response);
            return;
        }

        String wrappedKey = request.getHeader(Envelope.HEADER_KEY);
        if (wrappedKey == null || wrappedKey.isBlank()) {
            reject(response, "请求未加密：缺少 " + Envelope.HEADER_KEY + " 头");
            return;
        }

        byte[] aesKey;
        try {
            aesKey = crypto.unwrapKey(wrappedKey);
        } catch (CryptoException e) {
            reject(response, e.getMessage());
            return;
        }

        try {
            // ---- 1) 元信息：时间窗、nonce、令牌 ----
            String metaHeader = request.getHeader(Envelope.HEADER_META);
            if (metaHeader == null || metaHeader.isBlank()) {
                reject(response, "请求未加密：缺少 " + Envelope.HEADER_META + " 头");
                return;
            }

            JsonNode meta;
            try {
                Envelope metaEnv = mapper.readValue(
                        Base64.getDecoder().decode(metaHeader), Envelope.class);
                meta = mapper.readTree(crypto.decryptBody(aesKey, metaEnv.getIv(), metaEnv.getD()));
            } catch (CryptoException | IllegalArgumentException e) {
                reject(response, "元信息解密失败：" + e.getMessage());
                return;
            }

            verifyFreshness(meta);
            String token = meta.path("token").asText(null);

            // ---- 2) 请求体（可选）----
            byte[] body = new byte[0];
            if (hasBody(request)) {
                Envelope env;
                try {
                    env = mapper.readValue(request.getInputStream(), Envelope.class);
                } catch (IOException e) {
                    reject(response, "加密体格式不合法");
                    return;
                }
                body = crypto.decryptBody(aesKey, env.getIv(), env.getD())
                        .getBytes(StandardCharsets.UTF_8);
            }

            // ---- 3) 放行 ----
            HttpServletRequest working =
                    new DecryptedRequestWrapper(request, body, token);

            // 流式端点：不包装响应（否则缓冲会破坏流式），把密钥交接给控制器逐事件加密。
            // 交接的是副本 —— 过滤器 finally 里会清零自己那份，副本归控制器所有。
            if (isStreaming(request)) {
                request.setAttribute(ATTR_AES_KEY, aesKey.clone());
                chain.doFilter(working, response);
                return;
            }

            ContentCachingResponseWrapper cached = new ContentCachingResponseWrapper(response);
            chain.doFilter(working, cached);

            byte[] raw = cached.getContentAsByteArray();
            if (raw.length == 0) {
                cached.copyBodyToResponse();
                return;
            }

            Envelope out = crypto.encryptBody(aesKey, new String(raw, StandardCharsets.UTF_8));
            byte[] bytes = mapper.writeValueAsBytes(out);

            response.setStatus(cached.getStatus());
            response.setContentType("application/json;charset=UTF-8");
            response.setContentLength(bytes.length);
            response.setHeader(Envelope.HEADER_FLAG, "1");
            response.setHeader(Envelope.HEADER_IV, out.getIv());
            response.getOutputStream().write(bytes);

        } catch (CryptoException e) {
            reject(response, e.getMessage());
        } finally {
            HybridCryptoService.wipe(aesKey);
        }
    }

    private boolean isStreaming(HttpServletRequest request) {
        String uri = request.getRequestURI();
        for (String p : props.getStreamPathPatterns()) {
            if (matcher.match(p, uri)) {
                return true;
            }
        }
        return false;
    }

    private static boolean hasBody(HttpServletRequest request) {
        long len = request.getContentLengthLong();
        return len > 0 || "chunked".equalsIgnoreCase(request.getHeader("Transfer-Encoding"));
    }

    /** 时间窗 + nonce 双重防重放。nonce 依赖 Redis，不可用时降级为只校验时间窗并记警告。 */
    private void verifyFreshness(JsonNode meta) {
        long ts = meta.path("ts").asLong(0L);
        long tolerance = props.getTimestampToleranceSeconds() * 1000L;
        if (ts <= 0 || Math.abs(System.currentTimeMillis() - ts) > tolerance) {
            throw new CryptoException("请求已过期（时间戳超出允许范围），检查客户端与本机时钟是否一致");
        }

        String nonce = meta.path("nonce").asText(null);
        if (!props.isNonceCheck() || nonce == null || nonce.isBlank()) {
            return;
        }
        try {
            Boolean fresh = redis.opsForValue().setIfAbsent(
                    NONCE_PREFIX + nonce, "1",
                    Duration.ofSeconds(props.getNonceTtlSeconds()));
            if (Boolean.FALSE.equals(fresh)) {
                throw new CryptoException("请求重复提交（nonce 已使用）");
            }
        } catch (CryptoException e) {
            throw e;
        } catch (Exception e) {
            log.warn("nonce 校验跳过（Redis 不可用）：{}", e.getMessage());
        }
    }

    private void reject(HttpServletResponse response, String message) throws IOException {
        response.setStatus(HttpServletResponse.SC_BAD_REQUEST);
        response.setContentType("application/json;charset=UTF-8");
        byte[] body = mapper.writeValueAsBytes(
                java.util.Map.of("code", 40000, "message", message));
        response.setContentLength(body.length);
        response.getOutputStream().write(body);
    }
}
