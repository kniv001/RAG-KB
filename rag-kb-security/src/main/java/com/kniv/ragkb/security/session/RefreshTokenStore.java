package com.kniv.ragkb.security.session;

import com.kniv.ragkb.security.jwt.JwtProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.security.SecureRandom;
import java.time.Duration;
import java.util.Base64;
import java.util.Collections;
import java.util.Set;

/**
 * Refresh Token 与登录限速的 Redis 存储。
 *
 * <p>为什么 Refresh 放 Redis 而 Access 不放：Access 是 JWT，无状态、验签即可（零查询），
 * 代价是签发后无法撤回。所以给它短 TTL，把长期凭证交给 Redis 托管 —— 可以主动删除，
 * 于是「登出 / 改密码 / 踢下线」才真正生效。
 *
 * <p>键设计：
 * <pre>
 *   rt:&lt;token&gt;            -&gt; "username|ua|iat"        TTL = refreshTtl
 *   rt_user:&lt;username&gt;    -&gt; SET(该用户所有有效 token)
 *   loginfail:&lt;username&gt;  -&gt; 计数器                  TTL = loginLockSeconds
 * </pre>
 *
 * <p>兼容性：本机 Redis 是 3.0.504（2015 年版）。用到的是 SET EX / GET / SADD / SMEMBERS /
 * INCR / EXPIRE / TTL，都是 2.6 时代就有的命令。不用 Redis 做序列化对象存储，
 * 只存以竖线分隔的字符串，避免 JDK 序列化带来的类耦合与安全面。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class RefreshTokenStore {

    private static final String RT_PREFIX = "rt:";
    private static final String USER_PREFIX = "rt_user:";
    private static final String FAIL_PREFIX = "loginfail:";
    private static final String SEP = "|";

    private final StringRedisTemplate redis;
    private final JwtProperties props;

    public static String newToken() {
        byte[] b = new byte[32];
        new SecureRandom().nextBytes(b);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
    }

    // ---------------- Refresh Token ----------------

    public void save(String token, String username, String userAgent) {
        String value = username + SEP + normalize(userAgent) + SEP + System.currentTimeMillis();
        redis.opsForValue().set(RT_PREFIX + token, value, Duration.ofSeconds(props.getRefreshTtlSeconds()));
        String userKey = USER_PREFIX + username;
        redis.opsForSet().add(userKey, token);
        redis.expire(userKey, Duration.ofSeconds(props.getRefreshTtlSeconds()));
    }

    /** 返回用户名；token 不存在或已过期返回 null。 */
    public String resolve(String token) {
        if (token == null || token.isBlank()) {
            return null;
        }
        String value = redis.opsForValue().get(RT_PREFIX + token);
        if (value == null) {
            return null;
        }
        int i = value.indexOf(SEP);
        return i > 0 ? value.substring(0, i) : value;
    }

    public void delete(String token) {
        if (token == null || token.isBlank()) {
            return;
        }
        String username = resolve(token);
        redis.delete(RT_PREFIX + token);
        if (username != null) {
            redis.opsForSet().remove(USER_PREFIX + username, token);
        }
    }

    /** 吊销某用户全部会话（改密码 / 踢下线）。返回被吊销的数量。 */
    public long deleteAllFor(String username) {
        Set<String> tokens = members(USER_PREFIX + username);
        if (!tokens.isEmpty()) {
            redis.delete(tokens.stream().map(t -> RT_PREFIX + t).toList());
        }
        redis.delete(USER_PREFIX + username);
        return tokens.size();
    }

    public long countSessions(String username) {
        return members(USER_PREFIX + username).size();
    }

    public boolean ping() {
        try {
            return Boolean.TRUE.equals(redis.hasKey("__probe__")) || redis.getConnectionFactory() != null;
        } catch (Exception e) {
            return false;
        }
    }

    private Set<String> members(String key) {
        try {
            Set<String> s = redis.opsForSet().members(key);
            return s == null ? Collections.emptySet() : s;
        } catch (Exception e) {
            log.warn("读取 Redis 集合失败 {}：{}", key, e.getMessage());
            return Collections.emptySet();
        }
    }

    // ---------------- 登录限速 ----------------

    /** 返回剩余锁定秒数，0 表示未锁定。 */
    public long lockedSeconds(String username) {
        try {
            String v = redis.opsForValue().get(FAIL_PREFIX + username);
            long n = v == null ? 0 : Long.parseLong(v);
            if (n < props.getLoginMaxFails()) {
                return 0;
            }
            Long ttl = redis.getExpire(FAIL_PREFIX + username);
            return ttl == null || ttl < 0 ? props.getLoginLockSeconds() : ttl;
        } catch (Exception e) {
            return 0;
        }
    }

    public long noteFailure(String username) {
        try {
            String key = FAIL_PREFIX + username;
            Long n = redis.opsForValue().increment(key);
            redis.expire(key, Duration.ofSeconds(props.getLoginLockSeconds()));
            return n == null ? 0 : n;
        } catch (Exception e) {
            log.warn("登录失败计数写入失败：{}", e.getMessage());
            return 0;
        }
    }

    public void clearFailures(String username) {
        try {
            redis.delete(FAIL_PREFIX + username);
        } catch (Exception e) {
            log.warn("清除登录失败计数失败：{}", e.getMessage());
        }
    }

    private static String normalize(String s) {
        if (s == null) {
            return "";
        }
        String t = s.replace(SEP, "/").replace("\n", " ");
        return t.length() > 120 ? t.substring(0, 120) : t;
    }
}
