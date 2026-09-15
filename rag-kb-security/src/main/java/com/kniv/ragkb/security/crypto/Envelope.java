package com.kniv.ragkb.security.crypto;

import com.fasterxml.jackson.annotation.JsonInclude;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 加密载荷的线上格式。
 *
 * <p>请求：AES 密钥放在请求头 {@code X-Enc-Key}（RSA 包裹），
 * 有请求体时，体为 {@code {"iv":"..","d":".."}}。
 * 响应：体为 {@code {"iv":"..","d":".."}}，密钥同上（服务端换新 IV）。
 *
 * <p>为什么密钥走头而不是体：GET / DELETE 没有请求体，走头才能统一处理。
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
@JsonInclude(JsonInclude.Include.NON_NULL)
public class Envelope {

    /** 请求头名：RSA-OAEP 包裹后的 AES 密钥（base64） */
    public static final String HEADER_KEY = "X-Enc-Key";

    /**
     * 请求头名：加密后的元信息，值为 base64( {"iv":..,"d":..} )，
     * 其中明文为 {"ts":毫秒,"nonce":"..","token":"Bearer xxx 或裸 JWT"}。
     *
     * <p>为什么要单独一个头：GET / DELETE 没有请求体，若把令牌放在明文头里，
     * 中间设备照样能拿到 —— 加密就白做了。放这里所有方法就统一了。
     */
    public static final String HEADER_META = "X-Enc-Meta";
    /** 请求/响应头名：本次加密所用 IV（base64），便于日志排查 */
    public static final String HEADER_IV = "X-Enc-Iv";
    /** 标记头：表示这是加密载荷 */
    public static final String HEADER_FLAG = "X-Encrypted";

    /** base64( 12 字节 GCM IV ) */
    private String iv;

    /** base64( AES-256-GCM 密文，尾部含 16 字节认证标签 ) */
    private String d;
}
