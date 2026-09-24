package com.kniv.ragkb.service.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

/**
 * 联网搜索与抓取的参数。
 */
@Data
@ConfigurationProperties(prefix = "ragkb.web")
@Component
public class WebProperties {

    /**
     * 总开关。关掉后 /api/web/** 一律拒绝，前端入口也应当隐藏。
     *
     * <p>默认<b>关</b>：这个功能会让服务端主动向外发起请求，与「只监听本机、
     * 不开任何出站以外的口子」的默认姿态不同，应当由用户明确打开。
     */
    private boolean enabled = false;

    /**
     * 搜索后端，按顺序尝试，第一个有结果的胜出。
     *
     * <p>默认 360 在前、Bing 兜底。实测（本机校园网，查询「三层缓存架构」）：
     * Bing 会把「三层」拆成单字「三」返回汉字百科页（加 mkt、换 cn.bing.com
     * 都一样），而 360 结果准确、且真实网址直接写在页面属性里。
     * 英文查询则反过来，Bing 正常 —— 两个都留着互相兜底。
     *
     * <p>注意这里只能调顺序，加新后端要同时写解析逻辑（见 WebSearchService.Backend），
     * 所以它是个代码改动而不是配置改动。
     */
    private List<String> backends = List.of("so360", "bing");

    /**
     * **抓取路径的额外 CA**（classpath 资源名，逗号分隔）。
     *
     * <p>为什么需要：中文站点有一大批用 **CFCA** 签证书，而 JDK 的 cacerts 里一条都没有
     * ⇒ Java 侧一律 PKIX 失败（curl/浏览器读的是 Windows 证书库，所以能过）。
     * 这类 CA 只影响**抓取**，所以不导入 JDK 信任库（那会影响这台机器上所有 JVM 程序），
     * 只挂在抓取用的 HttpClient 上，见 {@code FetchHttp}。
     *
     * <p>⚠️ **必须按名列出**，不能"扫目录"：跑的是 fat jar，classpath 目录是 {@code jar:} 协议、
     * **列举不出内容**。第一版就是只写了目录列举，于是 jar 里那张 CFCA 证书**静默没被加载**，
     * 而日志还打了一句"没有额外 CA，只用 JDK 默认（这不会有任何副作用）" —— 看着一切正常。
     */
    private List<String> extraCa = List.of("certs/cfca-ev-root.cer");

    /** 抓取时伪装的 UA。用默认的 Java UA 很多站点会直接拒绝 */
    private String userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

    /** 单次请求超时（秒） */
    private int timeoutSeconds = 20;

    /** 单个页面最多读多少字节，防止撞上大文件把内存吃光 */
    private int maxPageBytes = 2_000_000;

    /** 一次搜索返回多少条结果 */
    private int maxResults = 8;

    /**
     * 连续抓取之间的间隔（毫秒）。
     *
     * <p>批量入库时会连着抓十几个页面。不间隔的话既是给对端添麻烦，
     * 也容易被判定成爬虫直接封掉。
     */
    private int fetchDelayMs = 400;

    /** 单篇正文的字符上限，超过就截断 —— 太长切分与向量化都吃不消 */
    private int maxTextChars = 60_000;

    /**
     * 可选的检索来源。选了某个来源，搜索就会带上 {@code site:} 限定，
     * 只在那个站里找 —— 不选则全网。
     *
     * <p>为什么要有这个：全网搜出来的东西质量参差，而不同的问题适合不同的来源。
     * 查概念该去百科，查实现该去技术社区。限定来源比在结果里挑省事得多。
     *
     * <p>默认这份清单是**实测可达**的站点，不是拍脑袋列的。见 {@code note} 字段。
     */
    private List<Source> sources = new ArrayList<>();

    @Data
    public static class Source {
        /** 界面上显示的名字 */
        private String label;
        /** 域名，用于拼 site: 限定 */
        private String domain;
        /** 备注（例如当前网络不可达），界面上会一并显示 */
        private String note;
    }
}
