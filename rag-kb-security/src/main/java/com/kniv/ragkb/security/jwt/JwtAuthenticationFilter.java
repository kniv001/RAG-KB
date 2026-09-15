package com.kniv.ragkb.security.jwt;

import io.jsonwebtoken.ExpiredJwtException;
import io.jsonwebtoken.JwtException;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpHeaders;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.authentication.WebAuthenticationDetailsSource;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.List;

/**
 * 从 Authorization: Bearer 头解出用户并写入 SecurityContext。
 *
 * <p>刻意<b>不</b>抛异常：令牌缺失/无效不是这个过滤器的职责，交给后面的授权规则判 401。
 * 只有一种情况要额外标记 —— 「过期」与「伪造」对客户端意义完全不同：
 * 前者应该去刷新令牌，后者应该跳登录页。用请求属性把它带出去，由 EntryPoint 写进响应体。
 */
@Slf4j
@RequiredArgsConstructor
public class JwtAuthenticationFilter extends OncePerRequestFilter {

    /** 请求属性名：令牌已过期（区别于令牌无效） */
    public static final String ATTR_EXPIRED = "ragkb.jwt.expired";

    private static final String BEARER = "Bearer ";

    private final JwtService jwtService;

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        String header = request.getHeader(HttpHeaders.AUTHORIZATION);
        if (header != null && header.regionMatches(true, 0, BEARER, 0, BEARER.length())) {
            String token = header.substring(BEARER.length()).trim();
            if (!token.isEmpty()) {
                try {
                    String username = jwtService.parseUsername(token);
                    var auth = new UsernamePasswordAuthenticationToken(
                            username, null, List.of(new SimpleGrantedAuthority("ROLE_USER")));
                    auth.setDetails(new WebAuthenticationDetailsSource().buildDetails(request));
                    SecurityContextHolder.getContext().setAuthentication(auth);
                } catch (ExpiredJwtException e) {
                    request.setAttribute(ATTR_EXPIRED, Boolean.TRUE);
                } catch (JwtException | IllegalArgumentException e) {
                    // 伪造、篡改、格式错 —— 不设置认证即可，细节不对外暴露
                    log.debug("令牌校验失败：{}", e.getMessage());
                }
            }
        }
        chain.doFilter(request, response);
    }
}
