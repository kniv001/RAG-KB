package com.kniv.ragkb.provider;

/**
 * 模型调用失败。属于「下游依赖不可用」，不是客户端错误 ——
 * 由全局异常处理转成 503，让前端知道「稍后重试」而不是「改请求」。
 */
public class ProviderException extends RuntimeException {

    private final String providerId;

    public ProviderException(String providerId, String message) {
        super(message);
        this.providerId = providerId;
    }

    public ProviderException(String providerId, String message, Throwable cause) {
        super(message, cause);
        this.providerId = providerId;
    }

    public String getProviderId() {
        return providerId;
    }
}
