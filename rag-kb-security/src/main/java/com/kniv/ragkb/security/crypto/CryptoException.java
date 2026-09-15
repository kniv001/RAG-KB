package com.kniv.ragkb.security.crypto;

/**
 * 加解密失败。一律视为客户端问题（公钥过期、密钥不匹配、密文被篡改），
 * 由全局异常处理转成 400，不回显底层细节以免成为攻击者的探针。
 */
public class CryptoException extends RuntimeException {

    public CryptoException(String message) {
        super(message);
    }

    public CryptoException(String message, Throwable cause) {
        super(message, cause);
    }
}
