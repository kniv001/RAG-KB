package com.kniv.ragkb.security.crypto;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 端到端加密配置。
 *
 * <p>为什么做这件事：Cloudflare 会终止 TLS，因此 CF 能看到经它转发的全部明文。
 * 应用层再做一层 RSA+AES 混合加密后，链路上的中间设备只能看到密文。
 *
 * <p>必须清楚的边界：前端 JS 本身也由 Cloudflare 分发，所以这层加密能做到
 * 「防中间设备偷看」，但做不到「防中间设备使坏（替换 JS）」。要完整端到端，
 * 客户端必须由自己控制（桌面端 / App）。
 */
@Data
@ConfigurationProperties(prefix = "ragkb.crypto")
public class CryptoProperties {

    /** 是否启用。关闭时所有接口按明文处理，便于本地调试。 */
    private boolean enabled = true;

    /** 密钥存放目录（相对启动目录），已 gitignore。 */
    private String keyDir = "data/keys";

    /** RSA 密钥长度。3072 位是当下的稳妥选择（2048 偏短，4096 慢）。 */
    private int rsaKeySize = 3072;

    /** 明文时间戳允许的偏差（秒），超出即判定为重放。 */
    private long timestampToleranceSeconds = 300;

    /** 是否启用 nonce 防重放（需要 Redis）。 */
    private boolean nonceCheck = true;

    /** nonce 在 Redis 里的保留时长（秒），应不小于 timestampToleranceSeconds。 */
    private long nonceTtlSeconds = 600;

    /** 免加密路径：公钥本身必须明文可取，否则无法引导。 */
    private String[] excludePaths = {
            "/api/crypto/public-key",
            "/api/whoami",
            "/actuator/**"
    };
}
