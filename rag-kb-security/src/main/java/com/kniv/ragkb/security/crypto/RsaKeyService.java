package com.kniv.ragkb.security.crypto;

import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.*;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.Base64;

/**
 * RSA 密钥对的加载/生成与持久化。
 *
 * <p>用 JDK 自带的 KeyFactory + PEM 手写读写，不引第三方库。
 * 私钥用 PKCS#8、公钥用 X.509/SPKI —— 这两个格式浏览器 WebCrypto 都能直接 importKey。
 *
 * <p>首次启动自动生成并落盘到 data/keys/（已 gitignore）。删除文件即重新生成一对新密钥，
 * 代价是客户端缓存的公钥失效（会自愈：客户端下次会重新拉公钥）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class RsaKeyService {

    private static final String PRIVATE_PEM = "rsa-private-pkcs8.pem";
    private static final String PUBLIC_PEM = "rsa-public-spki.pem";

    private final CryptoProperties props;

    private KeyPair keyPair;

    @PostConstruct
    public void init() throws Exception {
        Path dir = Path.of(props.getKeyDir());
        Files.createDirectories(dir);
        Path priv = dir.resolve(PRIVATE_PEM);
        Path pub = dir.resolve(PUBLIC_PEM);

        if (Files.exists(priv) && Files.exists(pub)) {
            keyPair = load(priv, pub);
            log.info("已加载 RSA 密钥对（{} 位），指纹 {}", props.getRsaKeySize(), fingerprint());
        } else {
            keyPair = generate();
            save(priv, pub);
            log.info("首次启动，已生成并保存 RSA 密钥对到 {}，指纹 {}", dir, fingerprint());
        }
    }

    /** 公钥的 SPKI 编码（base64），浏览器用 crypto.subtle.importKey('spki', ...) 直接吃。 */
    public String publicKeyBase64() {
        return Base64.getEncoder().encodeToString(keyPair.getPublic().getEncoded());
    }

    public PublicKey publicKey() {
        return keyPair.getPublic();
    }

    public PrivateKey privateKey() {
        return keyPair.getPrivate();
    }

    /** 公钥指纹（SHA-256 前 16 位 hex），用于客户端判断公钥是否换过。 */
    public String fingerprint() {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(keyPair.getPublic().getEncoded());
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 8; i++) {
                sb.append(String.format("%02x", d[i]));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            return "unknown";
        }
    }

    // ---------------- 内部 ----------------

    private KeyPair generate() throws NoSuchAlgorithmException {
        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(props.getRsaKeySize(), new SecureRandom());
        return gen.generateKeyPair();
    }

    private KeyPair load(Path priv, Path pub) throws Exception {
        KeyFactory kf = KeyFactory.getInstance("RSA");
        PrivateKey privateKey = kf.generatePrivate(
                new PKCS8EncodedKeySpec(readPem(priv)));
        PublicKey publicKey = kf.generatePublic(
                new X509EncodedKeySpec(readPem(pub)));
        return new KeyPair(publicKey, privateKey);
    }

    private void save(Path priv, Path pub) throws IOException {
        writePem(priv, "PRIVATE KEY", keyPair.getPrivate().getEncoded());
        writePem(pub, "PUBLIC KEY", keyPair.getPublic().getEncoded());
    }

    private static byte[] readPem(Path p) throws IOException {
        String s = Files.readString(p, StandardCharsets.US_ASCII);
        String base64 = s.replaceAll("-----[A-Z ]+-----", "").replaceAll("\\s", "");
        return Base64.getDecoder().decode(base64);
    }

    private static void writePem(Path p, String label, byte[] der) throws IOException {
        String body = Base64.getMimeEncoder(64, "\n".getBytes(StandardCharsets.US_ASCII))
                .encodeToString(der);
        Files.writeString(p, "-----BEGIN " + label + "-----\n" + body
                + "\n-----END " + label + "-----\n", StandardCharsets.US_ASCII);
    }
}
