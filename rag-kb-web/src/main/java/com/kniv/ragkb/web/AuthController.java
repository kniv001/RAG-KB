package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.domain.dto.AuthDtos;
import com.kniv.ragkb.security.AuthException;
import com.kniv.ragkb.security.jwt.JwtProperties;
import com.kniv.ragkb.security.service.AuthService;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseCookie;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.time.Duration;

/**
 * 认证接口。
 *
 * <p>响应体里<b>只有</b> access token；refresh token 一律走 httpOnly Cookie，
 * JS 读不到 —— 这样即使存在 XSS，也偷不走那把长期钥匙。
 *
 * <p>Cookie 的 Secure 标志按请求协议自动判定（看 X-Forwarded-Proto）：
 * 经 Cloudflare 是 https 就带，本机 http 调试不带，否则浏览器根本不发送。
 */
@RestController
@RequestMapping("/api/auth")
@RequiredArgsConstructor
public class AuthController {

    private final AuthService authService;
    private final JwtProperties jwtProps;

    @GetMapping("/config")
    public R<AuthDtos.AuthConfig> config() {
        return R.ok(authService.config());
    }

    @PostMapping("/login")
    public R<AuthDtos.TokenResponse> login(@RequestBody @Valid AuthDtos.LoginRequest body,
                                           HttpServletRequest req, HttpServletResponse res) {
        AuthService.Issued issued = authService.login(
                body.getUsername(), body.getPassword(), req.getHeader(HttpHeaders.USER_AGENT));
        writeRefreshCookie(req, res, issued.refreshToken());
        return R.ok(issued.response());
    }

    @PostMapping("/refresh")
    public R<AuthDtos.TokenResponse> refresh(HttpServletRequest req, HttpServletResponse res) {
        String token = readRefreshCookie(req);
        try {
            AuthService.Issued issued = authService.refresh(
                    token, req.getHeader(HttpHeaders.USER_AGENT));
            writeRefreshCookie(req, res, issued.refreshToken());   // 轮换：新的写回
            return R.ok(issued.response());
        } catch (AuthException e) {
            clearRefreshCookie(req, res);                          // 失效则清掉，避免反复重试
            throw e;
        }
    }

    @PostMapping("/logout")
    public R<Void> logout(HttpServletRequest req, HttpServletResponse res) {
        authService.logout(readRefreshCookie(req));
        clearRefreshCookie(req, res);
        return R.ok();
    }

    @GetMapping("/me")
    public R<AuthDtos.UserInfo> me(Authentication authentication) {
        if (authentication == null || !authentication.isAuthenticated()) {
            throw new AuthException("未登录");
        }
        return R.ok(authService.me(authentication.getName()));
    }

    @PostMapping("/password")
    public R<java.util.Map<String, Object>> changePassword(
            @RequestBody @Valid AuthDtos.ChangePasswordRequest body,
            Authentication authentication) {
        if (authentication == null || !authentication.isAuthenticated()) {
            throw new AuthException("未登录");
        }
        long revoked = authService.changePassword(
                authentication.getName(), body.getOldPassword(), body.getNewPassword());

        java.util.Map<String, Object> data = new java.util.LinkedHashMap<>();
        data.put("revokedSessions", revoked);
        data.put("note", "密码已更新，所有刷新令牌已吊销；"
                + "当前 access token 仍有效至自然过期（JWT 无状态的代价，≤30 分钟）");
        return R.ok(data);
    }

    // ---------------- Cookie ----------------

    private void writeRefreshCookie(HttpServletRequest req, HttpServletResponse res, String token) {
        ResponseCookie cookie = ResponseCookie.from(jwtProps.getRefreshCookieName(), token)
                .httpOnly(true)
                .secure(isSecure(req))
                .path(jwtProps.getRefreshCookiePath())
                .maxAge(Duration.ofSeconds(jwtProps.getRefreshTtlSeconds()))
                .sameSite("Strict")
                .build();
        res.addHeader(HttpHeaders.SET_COOKIE, cookie.toString());
    }

    private void clearRefreshCookie(HttpServletRequest req, HttpServletResponse res) {
        ResponseCookie cookie = ResponseCookie.from(jwtProps.getRefreshCookieName(), "")
                .httpOnly(true)
                .secure(isSecure(req))
                .path(jwtProps.getRefreshCookiePath())
                .maxAge(0)
                .sameSite("Strict")
                .build();
        res.addHeader(HttpHeaders.SET_COOKIE, cookie.toString());
    }

    private String readRefreshCookie(HttpServletRequest req) {
        Cookie[] cookies = req.getCookies();
        if (cookies == null) {
            return null;
        }
        for (Cookie c : cookies) {
            if (jwtProps.getRefreshCookieName().equals(c.getName())) {
                return c.getValue();
            }
        }
        return null;
    }

    private static boolean isSecure(HttpServletRequest req) {
        String proto = req.getHeader("X-Forwarded-Proto");
        return proto != null ? "https".equalsIgnoreCase(proto) : req.isSecure();
    }
}
