package com.kniv.ragkb.security;

/**
 * 认证/授权流程中的可预期失败（密码错误、账号锁定、刷新令牌失效等）。
 *
 * <p>与 {@code CryptoException} 一样，属于「客户端做错了」而不是「服务端崩了」，
 * 由全局异常处理转成 4xx，且只回显安全的信息，不泄露底层细节。
 */
public class AuthException extends RuntimeException {

    private final int code;

    public AuthException(String message) {
        this(message, 40100);
    }

    public AuthException(String message, int code) {
        super(message);
        this.code = code;
    }

    public int getCode() {
        return code;
    }

    /** 429：失败次数过多被锁定 */
    public static AuthException locked(String message) {
        return new AuthException(message, 42900);
    }

    /** 400：参数或业务校验失败 */
    public static AuthException badRequest(String message) {
        return new AuthException(message, 40000);
    }
}
