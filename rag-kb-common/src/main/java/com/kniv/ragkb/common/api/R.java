package com.kniv.ragkb.common.api;

import com.fasterxml.jackson.annotation.JsonInclude;
import lombok.Getter;

/**
 * 统一响应体。
 *
 * <p>约定：code = 0 表示成功，非 0 为业务错误码（与 HTTP 状态码解耦）。
 * 空字段不序列化，减少传输体积。
 */
@Getter
@JsonInclude(JsonInclude.Include.NON_NULL)
public class R<T> {

    /** 成功 */
    public static final int CODE_OK = 0;
    /** 参数/业务校验失败 */
    public static final int CODE_BAD_REQUEST = 40000;
    /** 未认证或令牌失效 */
    public static final int CODE_UNAUTHORIZED = 40100;
    /** 无权限 */
    public static final int CODE_FORBIDDEN = 40300;
    /** 资源不存在 */
    public static final int CODE_NOT_FOUND = 40400;
    /** 服务端内部错误 */
    public static final int CODE_ERROR = 50000;
    /** 下游依赖不可用（模型服务 / 数据库 / Redis） */
    public static final int CODE_DEPENDENCY = 50300;

    private final int code;
    private final String message;
    private final T data;

    private R(int code, String message, T data) {
        this.code = code;
        this.message = message;
        this.data = data;
    }

    public static <T> R<T> ok(T data) {
        return new R<>(CODE_OK, "ok", data);
    }

    public static R<Void> ok() {
        return new R<>(CODE_OK, "ok", null);
    }

    public static <T> R<T> fail(int code, String message) {
        return new R<>(code, message, null);
    }

    public static <T> R<T> fail(int code, String message, T data) {
        return new R<>(code, message, data);
    }

    public boolean isOk() {
        return code == CODE_OK;
    }
}
