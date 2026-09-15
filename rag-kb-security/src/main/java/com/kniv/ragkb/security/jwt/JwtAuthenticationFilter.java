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
import org.springframework.security.core.context.SecurityContext;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.authentication.WebAuthenticationDetailsSource;
import org.springframework.security.web.context.SecurityContextRepository;
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
    private final SecurityContextRepository contextRepository;

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

                    SecurityContext context = SecurityContextHolder.createEmptyContext();
                    context.setAuthentication(auth);
                    SecurityContextHolder.setContext(context);

                    // 必须显式保存：Spring Security 6 起不再自动把上下文写进仓库。
                    // 不保存的话，SSE 这类异步请求在 ASYNC 派发时（OncePerRequestFilter
                    // 默认跳过异步派发，本过滤器第二趟不会执行）会判定为未认证，
                    // 表现为「流跑到最后突然 500 / Access Denied」。
                    // 用 RequestAttributeSecurityContextRepository：上下文存请求属性，
                    // 而请求属性恰恰会跨异步派发保留下来。
                    contextRepository.saveContext(context, request, response);
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
