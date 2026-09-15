package com.kniv.ragkb.security.crypto;

import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.data.redis.core.StringRedisTemplate;

@Slf4j
@Configuration
@EnableConfigurationProperties(CryptoProperties.class)
public class CryptoConfig {

    /**
     * 注册加解密过滤器。
     *
     * <p>顺序刻意排在 Spring Security 之前（Security 默认 order = -100）：
     * 本过滤器负责把密文里的令牌注入成 Authorization 头，安全链才能读到。
     */
    @Bean
    public FilterRegistrationBean<EncryptedApiFilter> encryptedApiFilter(
            CryptoProperties props,
            HybridCryptoService crypto,
            ObjectMapper mapper,
            StringRedisTemplate redis) {

        EncryptedApiFilter filter = new EncryptedApiFilter(props, crypto, mapper, redis);
        FilterRegistrationBean<EncryptedApiFilter> reg = new FilterRegistrationBean<>(filter);
        reg.addUrlPatterns("/api/*");
        reg.setOrder(Ordered.HIGHEST_PRECEDENCE + 10);
        reg.setName("encryptedApiFilter");

        log.info("端到端加密过滤器已注册：enabled={}, 免加密路径={}",
                props.isEnabled(), String.join(",", props.getExcludePaths()));
        return reg;
    }
}
