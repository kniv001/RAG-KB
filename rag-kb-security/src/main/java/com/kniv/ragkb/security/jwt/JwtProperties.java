package com.kniv.ragkb.security.jwt;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

@Data
@ConfigurationProperties(prefix = "ragkb.jwt")
public class JwtProperties {

    /**
     * HS256 签名密钥（base64 或原始串）。留空则首次启动自动生成并写入 secretFile。
     * 刻意与传输加密用的 RSA 密钥分开 —— 同一对密钥承担两种用途是坏习惯。
     */
    private String secret = "";

    /** 密钥落盘位置（已 gitignore）。 */
    private String secretFile = "data/keys/jwt-secret.txt";

    private String issuer = "rag-kb";

    /** Access Token 有效期（秒）。短 TTL 是无状态 JWT 的代价补偿：登出后最多残留这么久。 */
    private long accessTtlSeconds = 1800;

    /** Refresh Token 有效期（秒）。 */
    private long refreshTtlSeconds = 2592000;

    /** 登录失败上限，超过则锁定。 */
    private int loginMaxFails = 10;

    /** 锁定时长（秒）。 */
    private long loginLockSeconds = 900;

    /** Refresh Cookie 名与作用路径。路径限定到认证接口，减少暴露面。 */
    private String refreshCookieName = "kb_refresh";
    private String refreshCookiePath = "/api/auth";
}
