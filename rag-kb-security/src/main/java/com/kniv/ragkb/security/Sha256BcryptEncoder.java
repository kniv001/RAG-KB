package com.kniv.ragkb.security;

import org.springframework.security.crypto.bcrypt.BCrypt;
import org.springframework.security.crypto.password.PasswordEncoder;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * 与 Python 版逐字节兼容的密码编码器。
 *
 * <p><b>为什么不能直接用 BCryptPasswordEncoder</b>：bcrypt 只取输入的前 72 字节，
 * 中文密码很容易超。Python 版的应对是先做一次 SHA-256，把任意长度的口令压成固定的
 * 32 字节再交给 bcrypt。Java 的 {@code BCryptPasswordEncoder} 则是直接把口令字符串
 * 按 UTF-8 喂进去 —— 两者输入根本不同，同一个密码算出的哈希对不上。
 *
 * <p>所以这里复刻 Python 的做法：SHA-256 摘要（原始 32 字节，不是 hex 也不是 base64）
 * → bcrypt。必须走 Spring 的 {@code BCrypt.hashpw(byte[], salt)} 重载：
 * 原始摘要含非 UTF-8 字节，用 String API 会被二次编码破坏。
 *
 * <p>存储格式不加 {@code {bcrypt}} 前缀 —— Python 的 bcrypt 直接读库里的串做校验，
 * 加了前缀它就不认识了。库里的值形如 {@code $2b$12$...}。
 */
public class Sha256BcryptEncoder implements PasswordEncoder {

    /** bcrypt 盐与哈希的长度：$2a$12$ 前缀 7 字符 + 22 字符盐 */
    private static final int SALT_LENGTH = 29;

    @Override
    public String encode(CharSequence rawPassword) {
        return BCrypt.hashpw(preHash(rawPassword), BCrypt.gensalt(SecurityConfig.BCRYPT_STRENGTH));
    }

    /**
     * 校验。等价于「用存储串里的盐重新哈希一遍再比对」——
     * bcrypt 的校验本来就是这么做的，Spring 的 checkpw 内部也是同一逻辑，
     * 只是它走 String 入口，这里必须走 byte[]。
     */
    @Override
    public boolean matches(CharSequence rawPassword, String encodedPassword) {
        if (encodedPassword == null || encodedPassword.length() < SALT_LENGTH) {
            return false;
        }
        String stored = stripDelegatingPrefix(encodedPassword);
        if (stored.length() < SALT_LENGTH) {
            return false;
        }
        try {
            String salt = stored.substring(0, SALT_LENGTH);
            return BCrypt.hashpw(preHash(rawPassword), salt).equals(stored);
        } catch (IllegalArgumentException e) {
            // 盐格式不合法（不是 bcrypt 哈希）
            return false;
        }
    }

    /** 兼容 {@code {bcrypt}$2b$...} 形式 —— 万一将来改用委托式编码器写入 */
    private static String stripDelegatingPrefix(String encoded) {
        if (encoded.startsWith("{") && encoded.contains("}")) {
            return encoded.substring(encoded.indexOf('}') + 1);
        }
        return encoded;
    }

    /**
     * SHA-256 摘要，返回<b>原始 32 字节</b>。
     * 不能转成 hex 或 base64 —— Python 传给 bcrypt 的是 digest() 的字节本身，多一层编码就对不上。
     */
    private static byte[] preHash(CharSequence rawPassword) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return md.digest(rawPassword.toString().getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("JVM 不支持 SHA-256", e);
        }
    }
}
