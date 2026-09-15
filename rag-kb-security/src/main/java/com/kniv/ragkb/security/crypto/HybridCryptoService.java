package com.kniv.ragkb.security.crypto;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.OAEPParameterSpec;
import javax.crypto.spec.PSource;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.spec.MGF1ParameterSpec;
import java.util.Arrays;
import java.util.Base64;

/**
 * RSA-OAEP + AES-256-GCM 混合加密。
 *
 * <p><b>关键互操作点</b>：Java 的 {@code RSA/ECB/OAEPWithSHA-256AndMGF1Padding} 默认用
 * <b>SHA-1</b> 做 MGF1，而浏览器 WebCrypto 的 RSA-OAEP(hash=SHA-256) 摘要与 MGF1 <b>都用 SHA-256</b>。
 * 不显式对齐就永远解不开。所以这里构造 OAEPParameterSpec 强制 MGF1 用 SHA-256。
 *
 * <p>密文布局与 WebCrypto 一致：AES-GCM 输出为 ciphertext||tag（16 字节标签在尾），
 * Java 的 Cipher.doFinal 同样是这个布局，无需转换。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class HybridCryptoService {

    private static final String RSA_TRANSFORM = "RSA/ECB/OAEPWithSHA-256AndMGF1Padding";
    private static final String AES_ALG = "AES";
    private static final String AES_TRANSFORM = "AES/GCM/NoPadding";
    private static final int GCM_IV_LEN = 12;
    private static final int GCM_TAG_BITS = 128;
    private static final int AES_KEY_BYTES = 32;

    /** 摘要与 MGF1 都用 SHA-256 —— 与浏览器 WebCrypto 对齐的关键。 */
    private static final OAEPParameterSpec OAEP_SHA256 = new OAEPParameterSpec(
            "SHA-256", "MGF1", MGF1ParameterSpec.SHA256, PSource.PSpecified.DEFAULT);

    private final RsaKeyService keyService;
    private final SecureRandom random = new SecureRandom();

    // ---------------- 密钥 ----------------

    /** 解开请求头里 RSA 包裹的 AES 密钥。用完务必 {@link #wipe} 清零。 */
    public byte[] unwrapKey(String wrappedKeyBase64) {
        if (wrappedKeyBase64 == null || wrappedKeyBase64.isBlank()) {
            throw new CryptoException("缺少加密密钥头 " + Envelope.HEADER_KEY);
        }
        try {
            Cipher c = Cipher.getInstance(RSA_TRANSFORM);
            c.init(Cipher.DECRYPT_MODE, keyService.privateKey(), OAEP_SHA256);
            byte[] key = c.doFinal(b64(wrappedKeyBase64));
            if (key.length != AES_KEY_BYTES) {
                wipe(key);
                throw new CryptoException("AES 密钥长度不是 256 位");
            }
            return key;
        } catch (CryptoException e) {
            throw e;
        } catch (Exception e) {
            throw new CryptoException("RSA 解密失败（公钥可能已轮换，客户端需重新拉取）", e);
        }
    }

    public static void wipe(byte[] key) {
        if (key != null) {
            Arrays.fill(key, (byte) 0);
        }
    }

    // ---------------- 体 ----------------

    public String decryptBody(byte[] aesKey, String ivBase64, String dataBase64) {
        if (ivBase64 == null || dataBase64 == null) {
            throw new CryptoException("加密体缺少 iv 或 d 字段");
        }
        try {
            Cipher c = Cipher.getInstance(AES_TRANSFORM);
            c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(aesKey, AES_ALG),
                    new GCMParameterSpec(GCM_TAG_BITS, b64(ivBase64)));
            return new String(c.doFinal(b64(dataBase64)), StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new CryptoException("AES-GCM 解密失败（密钥不匹配或密文被篡改）", e);
        }
    }

    /** 用同一把密钥加密响应，<b>换一个全新 IV</b>（GCM 下复用 IV 会直接泄露明文异或关系）。 */
    /**
     * 造一个用于流式解密的 Cipher。
     *
     * <p>为什么不让调用方直接用 {@code CipherInputStream}：那个类在 AEAD 上有个
     * 历史坑（JDK-8012631）—— 认证标签校验失败时它可能把异常吞掉、静默截断，
     * 于是「解出来一半的文件」被当成成功。这里让调用方自己做 update/doFinal 循环，
     * 标签不对会在 {@code doFinal} 上明确抛出。
     */
    public Cipher decryptCipher(byte[] aesKey, String ivBase64) {
        try {
            Cipher c = Cipher.getInstance(AES_TRANSFORM);
            c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(aesKey, AES_ALG),
                    new GCMParameterSpec(GCM_TAG_BITS, b64(ivBase64)));
            return c;
        } catch (GeneralSecurityException e) {
            throw new CryptoException("无法建立解密器：" + e.getMessage(), e);
        }
    }

    public Envelope encryptBody(byte[] aesKey, String plaintext) {
        byte[] iv = new byte[GCM_IV_LEN];
        random.nextBytes(iv);
        try {
            Cipher c = Cipher.getInstance(AES_TRANSFORM);
            c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(aesKey, AES_ALG),
                    new GCMParameterSpec(GCM_TAG_BITS, iv));
            byte[] ct = c.doFinal(plaintext.getBytes(StandardCharsets.UTF_8));
            Envelope out = new Envelope();
            out.setIv(Base64.getEncoder().encodeToString(iv));
            out.setD(Base64.getEncoder().encodeToString(ct));
            return out;
        } catch (Exception e) {
            throw new CryptoException("AES-GCM 加密失败", e);
        }
    }

    // ---------------- 自检 ----------------

    /** 往返自检：AES 加解密 + RSA 包裹/解包。启动时跑一次，把配置错误暴露在启动阶段。 */
    public boolean selfTest() {
        try {
            byte[] key = new byte[AES_KEY_BYTES];
            random.nextBytes(key);
            String msg = "rag-kb-selftest";
            Envelope env = encryptBody(key, msg);
            boolean aesOk = msg.equals(decryptBody(key, env.getIv(), env.getD()));

            PublicKey pub = keyService.publicKey();
            Cipher c = Cipher.getInstance(RSA_TRANSFORM);
            c.init(Cipher.ENCRYPT_MODE, pub, OAEP_SHA256);
            byte[] wrapped = c.doFinal(key);
            byte[] unwrapped = unwrapKey(Base64.getEncoder().encodeToString(wrapped));
            boolean rsaOk = Arrays.equals(key, unwrapped);

            wipe(key);
            wipe(unwrapped);
            return aesOk && rsaOk;
        } catch (Exception e) {
            log.error("加密自检失败: {}", e.getMessage(), e);
            return false;
        }
    }

    private static byte[] b64(String s) {
        try {
            return Base64.getDecoder().decode(s);
        } catch (IllegalArgumentException e) {
            throw new CryptoException("base64 解码失败", e);
        }
    }
}
