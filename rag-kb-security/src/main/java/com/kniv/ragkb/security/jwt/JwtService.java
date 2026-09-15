package com.kniv.ragkb.security.jwt;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.Date;

/**
 * Access Token 的签发与校验（JWT / HS256）。
 *
 * <p>选 HS256 而不是 RS256：本系统里签名方与验证方是同一个进程，非对称签名带来的
 * 「验证方不需要签名能力」这个好处在此不成立，反而多一对密钥要管。
 * 将来若把验证下沉到网关或让第三方验签，再换 RS256。
 *
 * <p>Access 无状态 —— 每个请求只验签，不查库不查 Redis。代价是签发后无法撤回，
 * 所以 TTL 给得短（30 分钟），把「能长期持有的那把钥匙」交给可撤销的 Refresh Token。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class JwtService {

    private static final String CLAIM_TYPE = "typ";
    private static final String TYPE_ACCESS = "access";

    private final JwtProperties props;

    private SecretKey key;

    @PostConstruct
    public void init() {
        String secret = props.getSecret();
        if (secret == null || secret.isBlank()) {
            secret = loadOrCreateSecret();
        }
        byte[] raw = decodeSecret(secret);
        if (raw.length < 32) {
            throw new IllegalStateException(
                    "JWT 密钥至少需要 256 位（32 字节），当前只有 " + raw.length + " 字节");
        }
        this.key = Keys.hmacShaKeyFor(raw);
        log.info("JWT 签名密钥已就绪（HS256，{} 位），access TTL {}s，refresh TTL {}s",
                raw.length * 8, props.getAccessTtlSeconds(), props.getRefreshTtlSeconds());
    }

    public String issueAccessToken(String username) {
        long now = System.currentTimeMillis();
        return Jwts.builder()
                .subject(username)
                .issuer(props.getIssuer())
                .issuedAt(new Date(now))
                .expiration(new Date(now + props.getAccessTtlSeconds() * 1000))
                .claim(CLAIM_TYPE, TYPE_ACCESS)
                .id(randomId())
                .signWith(key, Jwts.SIG.HS256)
                .compact();
    }

    /**
     * 校验并解出用户名。
     *
     * @throws JwtException 签名不符、已过期、签发者不匹配等
     */
    public String parseUsername(String token) {
        Claims claims = Jwts.parser()
                .verifyWith(key)
                .requireIssuer(props.getIssuer())
                .build()
                .parseSignedClaims(token)
                .getPayload();
        return claims.getSubject();
    }

    public static String randomId() {
        byte[] b = new byte[12];
        new SecureRandom().nextBytes(b);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
    }

    // ---------------- 密钥持久化 ----------------

    private String loadOrCreateSecret() {
        Path f = Path.of(props.getSecretFile());
        try {
            if (Files.exists(f)) {
                String s = Files.readString(f, StandardCharsets.US_ASCII).trim();
                if (!s.isBlank()) {
                    return s;
                }
            }
            Files.createDirectories(f.getParent());
            byte[] b = new byte[48];                 // 384 位，留足余量
            new SecureRandom().nextBytes(b);
            String generated = Base64.getEncoder().encodeToString(b);
            Files.writeString(f, generated, StandardCharsets.US_ASCII);
            log.info("首次启动，已生成 JWT 密钥并保存到 {}", f.toAbsolutePath());
            return generated;
        } catch (Exception e) {
            throw new IllegalStateException("读写 JWT 密钥文件失败：" + f.toAbsolutePath(), e);
        }
    }

    private static byte[] decodeSecret(String secret) {
        try {
            return Base64.getDecoder().decode(secret);
        } catch (IllegalArgumentException e) {
            // 不是 base64 就按原始字节用，允许用户直接填一串长口令
            return secret.getBytes(StandardCharsets.UTF_8);
        }
    }
}
