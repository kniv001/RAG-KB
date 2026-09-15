package com.kniv.ragkb.security.service;

import com.kniv.ragkb.dao.mapper.UserMapper;
import com.kniv.ragkb.domain.dto.AuthDtos;
import com.kniv.ragkb.domain.entity.User;
import com.kniv.ragkb.security.AuthException;
import com.kniv.ragkb.security.jwt.JwtProperties;
import com.kniv.ragkb.security.jwt.JwtService;
import com.kniv.ragkb.security.session.RefreshTokenStore;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;

/**
 * 认证业务：登录、刷新、登出、改密。
 *
 * <p>双令牌的分工：Access 是无状态 JWT（短 TTL，验签即用，不可撤回）；
 * Refresh 是不透明随机串，托管在 Redis（长 TTL，可主动吊销）。
 * 登出/改密删 Redis 记录即可让长期凭证立刻失效。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class AuthService {

    /** 用户名不存在时也拿它跑一次哈希比对，抹平「存在」与「不存在」的响应时间差 */
    private static final String TIMING_EQUALIZER =
            "$2a$12$C6UzMDM.H6dfI/f/IKcEe.9pXwS5DIVXPXwaL3Fv9PZZ3qLQFzZ4u";

    private final UserMapper users;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;
    private final RefreshTokenStore tokenStore;
    private final JwtProperties props;

    /** 登录成功后的产物：给客户端的数据 + 需要写进 httpOnly Cookie 的 refresh */
    public record Issued(AuthDtos.TokenResponse response, String refreshToken) {
    }

    public Issued login(String username, String rawPassword, String userAgent) {
        long locked = tokenStore.lockedSeconds(username);
        if (locked > 0) {
            throw AuthException.locked("失败次数过多，请 " + (locked / 60 + 1) + " 分钟后再试");
        }

        User user = users.findByUsername(username);
        boolean ok;
        if (user == null) {
            passwordEncoder.matches(rawPassword, TIMING_EQUALIZER);
            ok = false;
        } else {
            ok = Boolean.TRUE.equals(user.getIsActive())
                    && passwordEncoder.matches(rawPassword, user.getPasswordHash());
        }

        if (!ok) {
            long n = tokenStore.noteFailure(username);
            long left = Math.max(0, props.getLoginMaxFails() - n);
            throw new AuthException(left > 0
                    ? "用户名或密码错误（还可尝试 " + left + " 次）"
                    : "失败次数过多，账号已临时锁定");
        }

        tokenStore.clearFailures(username);
        users.touchLogin(username, OffsetDateTime.now());

        return issue(user.getUsername(), userAgent, user.getCreatedAt());
    }

    /**
     * 用 Cookie 里的 refresh 换新 access，并<b>轮换</b> refresh。
     * 轮换的意义：旧 token 立即作废，被窃取的旧 token 无法与正主同时使用。
     */
    public Issued refresh(String refreshToken, String userAgent) {
        String username = tokenStore.resolve(refreshToken);
        if (username == null) {
            throw new AuthException("刷新令牌无效或已过期");
        }
        User user = users.findByUsername(username);
        if (user == null || !Boolean.TRUE.equals(user.getIsActive())) {
            tokenStore.delete(refreshToken);
            throw new AuthException("账号不存在或已停用");
        }

        tokenStore.delete(refreshToken);
        Issued issued = issue(username, userAgent, user.getCreatedAt());
        tokenStore.clearFailures(username);
        return issued;
    }

    public void logout(String refreshToken) {
        tokenStore.delete(refreshToken);
    }

    /** 改密码并吊销该用户全部会话。返回被吊销的会话数。 */
    public long changePassword(String username, String oldPassword, String newPassword) {
        if (newPassword == null || newPassword.length() < 8) {
            throw AuthException.badRequest("新密码至少 8 位");
        }
        User user = users.findByUsername(username);
        if (user == null || !passwordEncoder.matches(oldPassword, user.getPasswordHash())) {
            throw AuthException.badRequest("原密码不正确");
        }
        if (oldPassword.equals(newPassword)) {
            throw AuthException.badRequest("新密码不能与原密码相同");
        }
        users.updatePassword(username, passwordEncoder.encode(newPassword));
        long revoked = tokenStore.deleteAllFor(username);
        log.info("用户 {} 修改密码，吊销 {} 个会话", username, revoked);
        return revoked;
    }

    public AuthDtos.UserInfo me(String username) {
        User user = users.findByUsername(username);
        if (user == null) {
            throw new AuthException("账号不存在");
        }
        return new AuthDtos.UserInfo(
                user.getUsername(),
                user.getCreatedAt(),
                user.getLastLoginAt(),
                tokenStore.countSessions(username));
    }

    public AuthDtos.AuthConfig config() {
        boolean redisOk;
        try {
            tokenStore.countSessions("__probe__");
            redisOk = true;
        } catch (Exception e) {
            redisOk = false;
        }
        return new AuthDtos.AuthConfig(
                props.getAccessTtlSeconds(),
                props.getRefreshTtlSeconds(),
                users.countAll() > 0,
                redisOk);
    }

    private Issued issue(String username, String userAgent, OffsetDateTime createdAt) {
        String access = jwtService.issueAccessToken(username);
        String refresh = RefreshTokenStore.newToken();
        tokenStore.save(refresh, username, userAgent);
        AuthDtos.UserInfo info = new AuthDtos.UserInfo(
                username, createdAt, OffsetDateTime.now(), tokenStore.countSessions(username));
        return new Issued(
                AuthDtos.TokenResponse.of(access, props.getAccessTtlSeconds(), info), refresh);
    }
}
