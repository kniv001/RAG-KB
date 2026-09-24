package com.kniv.ragkb.service.web;

import lombok.extern.slf4j.Slf4j;

import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.GZIPInputStream;

/**
 * **抓取专用的 HTTP 客户端**：JDK 默认的信任库 **∪** 本项目 {@code certs/} 下的额外 CA。
 *
 * <p><b>为什么需要它</b>（2026-09-24 实测）：中文站点有一大批用 **CFCA**（中国金融认证中心）
 * 签的证书 —— 中国政府网、新华网都在内 —— 而 **JDK 的 {@code cacerts} 里一条 CFCA 都没有**，
 * 于是 Java 侧一律 {@code PKIX path building failed}。curl 与浏览器能过，是因为它们读的是
 * **Windows 证书库**。对"接中文新闻源"来说这是一道**前置障碍**，不是某个站的偶发问题。
 *
 * <p><b>为什么不是把 CFCA 导入 JDK 的 cacerts</b>：那会改变**这台机器上所有 JVM 程序**的信任面，
 * 而且 JDK 一升级就可能被覆盖。这里的做法是 —— **只影响经由本类发出的请求**：
 * 其它任何走默认 TLS 的代码（数据库、Redis、Ollama、模型 API）一个字都不变。
 *
 * <p><b>信任范围写清楚</b>：本类信任 = <i>JDK 默认信任的</i> ∪ <i>{@code certs/} 目录里的</i>。
 * 目前那里只有一张 {@code cfca-ev-root.cer}（自签，有效期到 2029-12-31），
 * 来源是 **Windows 根证书库**（也就是 curl/浏览器信任它的同一个来源）。
 * 加新的 CA = 往 {@code certs/} 里丢一个 {@code .cer}，**不用改代码** —— 但要清楚：
 * 每加一张，就是让本进程接受更多签发者签的证书。
 *
 * <p>⚠️ 刻意**不**用 {@code Jsoup.connect}：jsoup 走的是 {@code HttpsURLConnection}，
 * 它的 SSLSocketFactory 只能**全局**设，那又回到"影响整个 JVM"。
 * {@code java.net.http.HttpClient} 允许**每个实例**带自己的 SSLContext，正好是这个需求。
 */
@Slf4j
public final class FetchHttp {

    /** 额外 CA 所在目录（classpath）。**跑 jar 时列举不到**，所以真正的来源是配置里的名单。 */
    private static final String EXTRA_CA_DIR = "certs";

    /** 配置（由 WebSearchService 在建客户端前注入）。 */
    private static volatile List<String> extraCaNames = List.of("certs/cfca-ev-root.cer");

    /** 额外 CA 的信任管理器（懒建 —— 名单要等配置注入之后才能读）。 */
    private static volatile X509TrustManager extraTm;
    private static volatile HttpClient client;

    private static HttpClient client() {
        if (client == null) {
            synchronized (FetchHttp.class) {
                if (client == null) {
                    extraTm = buildExtraTrustManager();
                    client = HttpClient.newBuilder()
                            .connectTimeout(Duration.ofSeconds(10))
                            .followRedirects(HttpClient.Redirect.NORMAL)
                            .sslContext(buildContext())
                            .build();
                }
            }
        }
        return client;
    }

    private FetchHttp() {
    }

    /** 一次抓取的结果。**最终 URL 要带出来** —— 重定向之后必须再查一次 SSRF。 */
    public record Result(String finalUrl, String contentType, String body) {
    }

    public static Result get(String url, String userAgent, int timeoutSeconds, int maxBytes)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(timeoutSeconds))
                .header("User-Agent", userAgent)
                .header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
                // **不主动要 gzip**：HttpClient 不会自动解压，而漏解压的症状是"正文变成乱码"
                // （看起来像编码问题，其实是二进制）。万一服务端还是压缩了，下面兜一手。
                .GET()
                .build();
        HttpResponse<InputStream> res = client().send(req, HttpResponse.BodyHandlers.ofInputStream());
        String ct = res.headers().firstValue("content-type").orElse("");
        rejectBinary(ct);
        String enc = res.headers().firstValue("content-encoding").orElse("");
        try (InputStream in = res.body()) {
            InputStream src = enc.toLowerCase().contains("gzip") ? new GZIPInputStream(in) : in;
            byte[] bytes = readCapped(src, maxBytes);
            java.nio.charset.Charset charset = charsetOf(ct);
            return new Result(String.valueOf(res.uri()), ct, new String(bytes, charset));
        }
    }

    /**
     * 明确的二进制直接拒 —— 与原来 jsoup 的 {@code ignoreContentType(false)} 同一个用意：
     * 别把图片/压缩包当文本读进来，那会变成一堆乱码进库。
     */
    private static void rejectBinary(String contentType) {
        String ct = contentType == null ? "" : contentType.toLowerCase();
        if (ct.startsWith("image/") || ct.startsWith("video/") || ct.startsWith("audio/")
                || ct.contains("application/zip") || ct.contains("application/pdf")
                || ct.contains("application/octet-stream")) {
            throw new IllegalStateException("不是文本内容：" + contentType);
        }
    }

    private static byte[] readCapped(InputStream in, int max) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            if (out.size() + n > max) {
                out.write(buf, 0, max - out.size());
                break;   // 截断而不是报错 —— 与原来 maxBodySize 的行为一致
            }
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }

    private static java.nio.charset.Charset charsetOf(String contentType) {
        if (contentType != null) {
            java.util.regex.Matcher m =
                    java.util.regex.Pattern.compile("charset=([\\w-]+)").matcher(contentType);
            if (m.find()) {
                try {
                    return java.nio.charset.Charset.forName(m.group(1));
                } catch (Exception ignore) {
                    // 认不出来就用默认
                }
            }
        }
        // 中文站点里 GBK/GB18030 仍不少见；但**不在这里猜** ——
        // 页面的真实编码交给 jsoup 解析时按 meta 判定（它比响应头靠谱得多）。
        return StandardCharsets.UTF_8;
    }

    private static SSLContext buildContext() {
        try {
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, new TrustManager[]{new Composite(extraTm)}, null);
            return ctx;
        } catch (Exception e) {
            throw new IllegalStateException("建 SSLContext 失败：" + e.getMessage(), e);
        }
    }

    /** 额外 CA 的信任管理器（可能为空 —— 那样组合里只剩默认那一个）。 */
    private static X509TrustManager buildExtraTrustManager() {
        try {
            List<X509Certificate> extra = loadExtraCas();
            if (extra.isEmpty()) {
                log.info("抓取信任库：certs/ 下没有额外 CA，只用 JDK 默认（这不会有任何副作用）");
                return null;
            }
            KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
            ks.load(null, null);
            for (int i = 0; i < extra.size(); i++) {
                ks.setCertificateEntry("extra-" + i, extra.get(i));
            }
            TrustManagerFactory tmf =
                    TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(ks);
            StringBuilder names = new StringBuilder();
            for (X509Certificate c : extra) {
                names.append(c.getSubjectX500Principal().getName()).append("；");
            }
            log.info("抓取信任库：JDK 默认 ∪ {} 张额外 CA（{}）", extra.size(), names);
            return first(tmf.getTrustManagers());
        } catch (Exception e) {
            log.warn("额外 CA 加载失败，退回只用 JDK 默认信任库：{}", e.getMessage());
            return null;
        }
    }

    /** 由配置注入额外 CA 的名单（在建 HttpClient 之前调用）。 */
    public static void configure(List<String> names) {
        if (names != null && !names.isEmpty()) {
            extraCaNames = List.copyOf(names);
        }
    }

    /**
     * 加载额外 CA：**按名单**（jar 里唯一可行的方式），再对 {@code certs/} 目录做一次列举兜底
     * （开发时从 classpath 目录跑，丢个文件进去就能生效 —— 跑 jar 时那一步自然为空）。
     */
    private static List<X509Certificate> loadExtraCas() throws Exception {
        List<X509Certificate> out = new ArrayList<>();
        CertificateFactory cf = CertificateFactory.getInstance("X.509");
        ClassLoader cl = FetchHttp.class.getClassLoader();

        for (String name : extraCaNames) {
            try (InputStream in = cl.getResourceAsStream(name)) {
                if (in == null) {
                    log.warn("额外 CA 找不到：{}（名单来自 ragkb.web.extra-ca）", name);
                    continue;
                }
                out.addAll(cf.generateCertificates(in).stream()
                        .map(c -> (X509Certificate) c).toList());
            }
        }

        java.net.URL dir = cl.getResource(EXTRA_CA_DIR);
        if (dir != null && "file".equals(dir.getProtocol())) {
            java.io.File[] files = new java.io.File(dir.toURI())
                    .listFiles(f -> {
                        String n = f.getName().toLowerCase();
                        return n.endsWith(".cer") || n.endsWith(".crt") || n.endsWith(".pem");
                    });
            if (files != null) {
                for (java.io.File f : files) {
                    if (out.size() > 0 && extraCaNames.stream()
                            .anyMatch(n -> n.endsWith(f.getName()))) {
                        continue;   // 名单里已经有了，别加两遍
                    }
                    try (InputStream in = new java.io.FileInputStream(f)) {
                        out.addAll(cf.generateCertificates(in).stream()
                                .map(c -> (X509Certificate) c).toList());
                    }
                }
            }
        }
        return out;
    }

    private static X509TrustManager defaultTm() throws Exception {
        TrustManagerFactory tmf =
                TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init((KeyStore) null);      // null = 用 JDK 默认信任库
        return first(tmf.getTrustManagers());
    }

    private static X509TrustManager first(TrustManager[] tms) {
        for (TrustManager t : tms) {
            if (t instanceof X509TrustManager x) {
                return x;
            }
        }
        throw new IllegalStateException("拿不到 X509TrustManager");
    }

    /**
     * **组合**：任一侧认可即通过。
     *
     * <p>写成"先默认、默认不认再问额外"而不是合并信任库，是为了让**默认那套保持原样** ——
     * 额外 CA 只能**增加**可接受的签发者，不会让任何一个原本可信的变得不可信
     * （顺序反过来就会在默认信任库出错时误判）。
     */
    private static final class Composite implements X509TrustManager {
        private final X509TrustManager dflt;
        private final X509TrustManager extra;

        Composite(X509TrustManager extra) throws Exception {
            this.dflt = defaultTm();
            this.extra = extra;
        }

        @Override
        public void checkClientTrusted(X509Certificate[] chain, String authType)
                throws java.security.cert.CertificateException {
            dflt.checkClientTrusted(chain, authType);
        }

        @Override
        public void checkServerTrusted(X509Certificate[] chain, String authType)
                throws java.security.cert.CertificateException {
            try {
                dflt.checkServerTrusted(chain, authType);
            } catch (java.security.cert.CertificateException first) {
                if (extra == null) {
                    throw first;
                }
                try {
                    extra.checkServerTrusted(chain, authType);
                } catch (java.security.cert.CertificateException second) {
                    // 两边都不认：抛**默认那条**（它才是"为什么连不上"的常规解释），
                    // 但把额外那条也带上 —— 排查时能一眼看出试过哪些。
                    throw new java.security.cert.CertificateException(
                            first.getMessage() + " ／ 额外 CA 也不认：" + second.getMessage(), first);
                }
            }
        }

        @Override
        public X509Certificate[] getAcceptedIssuers() {
            X509Certificate[] a = dflt.getAcceptedIssuers();
            X509Certificate[] b = extra == null ? new X509Certificate[0] : extra.getAcceptedIssuers();
            X509Certificate[] all = new X509Certificate[a.length + b.length];
            System.arraycopy(a, 0, all, 0, a.length);
            System.arraycopy(b, 0, all, a.length, b.length);
            return all;
        }
    }
}
