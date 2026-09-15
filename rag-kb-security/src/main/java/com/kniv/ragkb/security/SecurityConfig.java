package com.kniv.ragkb.security;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.factory.PasswordEncoderFactories;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;

import jakarta.servlet.http.HttpServletResponse;
import org.springframework.security.web.SecurityFilterChain;

/**
 * 安全链配置。
 *
 * <p><b>当前是阶段占位</b>：鉴权用 Spring Security 自带的 HTTP Basic
 * （账号来自 spring.security.user.* 配置），目的是让加密链路可以先独立验证。
 * 下一步的 JWT + Redis 双令牌模块落地后，这里会换成：
 * 无状态 Bearer 过滤器 + 自定义 AuthenticationEntryPoint。
 *
 * <p>已经确定下来的部分保留：关闭 CSRF（无状态 API）、关闭 Session、
 * BCrypt 作为密码编码器（与 Python 版的 bcrypt 工作因子一致）。
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    /** 与 Python 版保持一致的工作因子 12 */
    public static final int BCRYPT_STRENGTH = 12;

    /**
     * 委托式编码器：按密码前缀选择算法。
     *
     * <p>用它是为了阶段过渡期两端都能跑：配置里的临时账号用 {@code {noop}} 前缀（明文，
     * 仅本地开发），而 users 表里存的 {@code {bcrypt}$2a$...} 也识别。
     * JWT 模块落地后临时账号会删除，只留 bcrypt。
     */
    @Bean
    public PasswordEncoder passwordEncoder() {
        return PasswordEncoderFactories.createDelegatingPasswordEncoder();
    }

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http
                // 无状态 API：不用 Cookie 承载会话，CSRF 无立足点
                .csrf(csrf -> csrf.disable())
                .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        // 公钥必须明文可取，否则加密无法引导
                        .requestMatchers("/api/crypto/public-key").permitAll()
                        .requestMatchers("/api/ping", "/api/whoami").permitAll()
                        .requestMatchers("/actuator/health").permitAll()
                        // 不放过 /error 的话，任何 404 都会被转发到 /error 再判未认证，
                        // 结果是「路径不存在」被伪装成「401 未认证」，非常难排查
                        .requestMatchers("/error").permitAll()
                        .anyRequest().authenticated())
                // 自定义 401：直接写 JSON 体，不用 sendError ——
                // sendError 会触发容器错误分发，绕过响应包装器，导致 401 体不被加密
                .exceptionHandling(e -> e.authenticationEntryPoint(
                        (req, res, ex) -> {
                            res.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
                            res.setContentType("application/json;charset=UTF-8");
                            res.getWriter().write(
                                    "{\"code\":40100,\"message\":\"未认证或令牌无效\"}");
                        }))
                // 【临时】JWT 双令牌模块落地后，改为 addFilterBefore(jwtFilter, ...)
                .httpBasic(Customizer.withDefaults());
        return http.build();
    }
}
