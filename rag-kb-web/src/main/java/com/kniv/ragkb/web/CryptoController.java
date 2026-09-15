package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.security.crypto.CryptoProperties;
import com.kniv.ragkb.security.crypto.HybridCryptoService;
import com.kniv.ragkb.security.crypto.RsaKeyService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 端到端加密的引导接口。
 *
 * <p>客户端流程：
 * <ol>
 *   <li>GET /api/crypto/public-key 拿公钥（免认证，否则无从引导）</li>
 *   <li>本地生成 AES-256 密钥，用公钥 RSA-OAEP(SHA-256) 包裹</li>
 *   <li>后续每个请求带 X-Enc-Key 头，体为 {"iv":..,"d":..}</li>
 * </ol>
 */
@RestController
@RequestMapping("/api/crypto")
@RequiredArgsConstructor
public class CryptoController {

    private final RsaKeyService keyService;
    private final HybridCryptoService crypto;
    private final CryptoProperties props;

    @GetMapping("/public-key")
    public R<Map<String, Object>> publicKey() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("enabled", props.isEnabled());
        body.put("algorithm", "RSA-OAEP-256");
        body.put("keySize", props.getRsaKeySize());
        // 指纹用于客户端判断公钥是否轮换过
        body.put("fingerprint", keyService.fingerprint());
        // SPKI 编码的 base64，浏览器 crypto.subtle.importKey('spki', ...) 直接可用
        body.put("publicKey", keyService.publicKeyBase64());
        return R.ok(body);
    }

    /** 服务端加密栈自检：AES-GCM 往返 + RSA 包裹/解包。 */
    @GetMapping("/selftest")
    public R<Map<String, Object>> selftest() {
        boolean ok = crypto.selfTest();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("aes", ok);
        body.put("rsa", ok);
        body.put("fingerprint", keyService.fingerprint());
        return ok ? R.ok(body) : R.fail(R.CODE_ERROR, "加密自检未通过");
    }
}
