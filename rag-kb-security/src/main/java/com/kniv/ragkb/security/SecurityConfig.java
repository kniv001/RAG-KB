package com.kniv.ragkb.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.security.crypto.CryptoProperties;
import com.kniv.ragkb.security.jwt.JwtAuthenticationFilter;
import com.kniv.ragkb.security.jwt.JwtService;
import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.MediaType;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 安全链：无状态 Bearer 鉴权。
 *
 * <p>与 Python 版对齐的几点：
 * <ul>
 *   <li>Access 走 Authorization 头，无状态，不建 Session</li>
 *   <li>Refresh 走 httpOnly Cookie，因此关闭 CSRF 的同时只对认证接口放行 Cookie 路径</li>
 *   <li>401 体里带 {@code expired} 标记，前端据此决定「去刷新」还是「跳登录」</li>
 * </ul>
 *
 * <p>执行顺序上，加解密过滤器（order 极小）先跑，把密文里的令牌注入成 Authorization 头；
 * 本链里的 JWT 过滤器随后读取，因此加密与鉴权是串联而非互相干扰的。
 */
@Configuration
@EnableWebSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    /** 与 Python 版保持一致的工作因子 12 */
    public static final int BCRYPT_STRENGTH = 12;

    private final JwtService jwtService;
    private final ObjectMapper objectMapper;
    private final CryptoProperties cryptoProperties;

    /**
     * 与 Python 版逐字节兼容的编码器：SHA-256 预哈希 → bcrypt。
     *
     * <p>不用现成的 {@code BCryptPasswordEncoder}，因为它的输入是原始口令字符串，
     * 而 Python 版喂给 bcrypt 的是 SHA-256 摘要 —— 同一个密码两边算出的哈希不同，
     * 共用同一个 users 表时会导致「Python 设的密码 Java 登不上」。
     */
    @Bean
    public PasswordEncoder passwordEncoder() {
        return new Sha256BcryptEncoder();
    }

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http
                // 无状态 API：认证靠 Bearer 头，不靠 Cookie 会话，CSRF 无立足点
                .csrf(csrf -> csrf.disable())
                .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        // 公钥必须明文可取，否则加密无从引导
                        .requestMatchers("/api/crypto/public-key").permitAll()
                        // 登录/刷新/登出/登录页配置：未认证也必须能访问
                        .requestMatchers("/api/auth/login", "/api/auth/refresh",
                                "/api/auth/logout", "/api/auth/config").permitAll()
                        .requestMatchers("/api/ping", "/api/whoami").permitAll()
                        .requestMatchers("/actuator/health").permitAll()
                        // 不放过 /error 的话，任何 404 都会被转发到 /error 再判未认证，
                        // 结果是「路径不存在」被伪装成「401 未认证」，极难排查
                        .requestMatchers("/error").permitAll()
                        .anyRequest().authenticated())
                // 直接写 JSON 体，不用 sendError —— sendError 会触发容器错误分发，
                // 绕过响应包装器，导致 401 不被加密
                .exceptionHandling(e -> e.authenticationEntryPoint(
                        (req, res, ex) -> writeUnauthorized(req, res)))
                .addFilterBefore(new JwtAuthenticationFilter(jwtService),
                        UsernamePasswordAuthenticationFilter.class);
        return http.build();
    }

    private void writeUnauthorized(jakarta.servlet.http.HttpServletRequest req,
                                   jakarta.servlet.http.HttpServletResponse res) throws java.io.IOException {
        boolean expired = Boolean.TRUE.equals(req.getAttribute(JwtAuthenticationFilter.ATTR_EXPIRED));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", expired ? 40101 : 40100);
        body.put("message", expired ? "登录已过期，请刷新令牌" : "未认证或令牌无效");
        body.put("expired", expired);

        res.setStatus(jakarta.servlet.http.HttpServletResponse.SC_UNAUTHORIZED);
        res.setContentType(MediaType.APPLICATION_JSON_VALUE);
        res.setCharacterEncoding("UTF-8");
        res.getWriter().write(objectMapper.writeValueAsString(body));
    }
}
