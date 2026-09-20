package com.kniv.ragkb.service.web;

import com.kniv.ragkb.service.config.WebProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.net.InetAddress;
import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * 联网搜索与网页正文抓取。
 *
 * <p><b>为什么不用搜索 API</b>：不需要密钥。代价是要解析各家搜索页的 HTML，
 * 对方改版就会坏 —— 所以做成了多后端按顺序尝试。
 *
 * <p><b>后端选择是实测定的，不是拍的</b>（本机校园网，查询「三层缓存架构」）：
 * <table>
 *   <tr><th>后端</th><th>结果</th></tr>
 *   <tr><td>Bing</td><td>❌ 把「三层」拆成单字「三」，返回汉字百科页。
 *       加了 {@code mkt}/{@code setmkt}/{@code cn.bing.com} 全都一样 —— 编码没问题
 *       （回显的码点确认收到的就是完整查询），是它自己切词坏了。英文查询正常</td></tr>
 *   <tr><td>搜狗</td><td>✅ 结果最准，但链接是 {@code /link?url=...} 的 JS 跳转，
 *       解析不出真实网址，入库时拿不到出处，只能放弃</td></tr>
 *   <tr><td><b>360</b></td><td>✅ 结果准，且真实网址直接写在 {@code data-mdurl} 属性里，
 *       不用额外请求</td></tr>
 *   <tr><td>百度</td><td>❌ 返回 1.4KB 反爬页</td></tr>
 *   <tr><td>DuckDuckGo / Brave / Jina / 维基</td><td>❌ 直接连不上</td></tr>
 * </table>
 *
 * <p><b>抓取任意 URL 是 SSRF 风险</b>，而这里尤其要紧：本机的 Redis 监听在
 * 0.0.0.0:6379，应用自己在 127.0.0.1:8080。一条搜索结果完全可能指向内网地址。
 * 所以每次抓取前都做地址检查，见 {@link #assertPublicHost}。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class WebSearchService {

    private final WebProperties props;

    /** 一条搜索结果 */
    public record WebHit(String title, String url, String snippet, String backend) {
    }

    /** 抓下来的一个页面 */
    public record FetchedPage(String url, String title, String text) {
    }

    /** 支持的搜索后端。加一个需要同时写解析逻辑，所以配置只能调顺序、不能加新的。 */
    public enum Backend {
        SO360("https://www.so.com/s", "q"),
        BING("https://www.bing.com/search", "q"),
        SOGOU("https://www.sogou.com/web", "query");

        final String endpoint;
        final String param;

        Backend(String endpoint, String param) {
            this.endpoint = endpoint;
            this.param = param;
        }
    }

    // ---------------- 搜索 ----------------

    /**
     * 按配置顺序尝试各后端，第一个返回结果的胜出。
     *
     * <p>而不是「合并所有后端的结果」：不同后端的排序不可比，合并需要另一套
     * 融合逻辑（检索那边用 RRF 解决的就是同类问题）。这里的场景是「哪个能用用哪个」，
     * 简单按顺序取第一个能用的就够。
     */
    public List<WebHit> search(String query, Integer count) {
        return search(query, count, null);
    }

    /**
     * @param site 限定域名（如 {@code blog.csdn.net}）。非空时拼成 {@code site:域名}
     *             加在查询后面 —— 实测 360 支持这个限定（10 条里 8 条来自指定站）。
     */
    public List<WebHit> search(String query, Integer count, String site) {
        requireEnabled();
        if (query == null || query.isBlank()) {
            return List.of();
        }
        String effective = query;
        if (site != null && !site.isBlank()) {
            // 域名单词化，避免用户从配置里带进奇怪的字符拼坏查询。
            // 正则里的连字符要写成 \\- —— Java 字符串里 \− 是非法转义（编译期就报）
            String d = site.trim().replaceAll("[^A-Za-z0-9.\\-]", "");
            if (!d.isEmpty()) {
                effective = query + " site:" + d;
            }
        }
        int n = count == null || count <= 0 ? props.getMaxResults()
                : Math.min(count, props.getMaxResults());

        List<String> failures = new ArrayList<>();
        for (String name : props.getBackends()) {
            Backend b;
            try {
                b = Backend.valueOf(name.trim().toUpperCase());
            } catch (IllegalArgumentException e) {
                log.warn("不认识的后端 {}，跳过（支持的：{}）", name,
                        java.util.Arrays.toString(Backend.values()));
                continue;
            }
            try {
                List<WebHit> hits = searchOne(b, effective, n);
                if (!hits.isEmpty()) {
                    log.debug("联网搜索「{}」{}用 {} 得到 {} 条", query,
                            site == null || site.isBlank() ? "" : "（限 " + site + "）", b, hits.size());
                    return hits;
                }
                failures.add(b + ":无结果");
            } catch (Exception e) {
                log.warn("后端 {} 搜索失败：{}", b, e.getMessage());
                failures.add(b + ":" + e.getMessage());
            }
        }
        // 全挂了要能说清是哪个环节挂的，而不是只回一个空列表
        throw new IllegalStateException("所有搜索后端都没拿到结果 —— " + String.join("；", failures));
    }

    private List<WebHit> searchOne(Backend b, String query, int n) throws IOException {
        String url = b.endpoint + "?" + b.param + "="
                + URLEncoder.encode(query, StandardCharsets.UTF_8)
                + ("bing".equalsIgnoreCase(b.name()) ? "&count=" + n * 2 : "");
        Document doc = fetchDocument(url);

        List<WebHit> hits = switch (b) {
            case SO360 -> parseSo360(doc, n);
            case BING -> parseBing(doc, n);
            case SOGOU -> parseSogou(doc, n);
        };
        return hits;
    }

    private List<WebHit> parseSo360(Document doc, int n) {
        List<WebHit> out = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        for (Element li : doc.select("li.res-list")) {
            Element a = li.selectFirst("h3.res-title a");
            if (a == null) {
                continue;
            }
            // 真实网址在 data-mdurl 里；href 是 so.com 的跳转链接，别用那个
            String href = a.attr("data-mdurl");
            if (href.isBlank()) {
                href = a.attr("abs:href");
            }
            if (href.isBlank() || !seen.add(href)) {
                continue;
            }
            Element p = li.selectFirst(".res-list-summary");
            out.add(new WebHit(clean(a.text()), href, p == null ? "" : clean(p.text()), "so360"));
            if (out.size() >= n) {
                break;
            }
        }
        return out;
    }

    private List<WebHit> parseBing(Document doc, int n) {
        List<WebHit> out = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        for (Element li : doc.select("li.b_algo")) {
            Element a = li.selectFirst("h2 a[href]");
            if (a == null) {
                continue;
            }
            String href = a.attr("abs:href");
            if (href.isBlank() || !seen.add(href)) {
                continue;
            }
            Element p = li.selectFirst(".b_caption p");
            if (p == null) {
                p = li.selectFirst("p");
            }
            out.add(new WebHit(clean(a.text()), href, p == null ? "" : clean(p.text()), "bing"));
            if (out.size() >= n) {
                break;
            }
        }
        return out;
    }

    /**
     * 搜狗做兜底。
     *
     * <p>它的结果质量最好，但链接是 {@code /link?url=...} 的 JS 跳转 ——
     * 实测直接请求那个地址不会跳到目标页（HTTP 200 但停在原地），
     * 所以拿不到真实网址、也就没法入库。只有当前面的后端都挂时才用它，
     * 且此时结果里的 url 字段对入库不可用。
     */
    private List<WebHit> parseSogou(Document doc, int n) {
        List<WebHit> out = new ArrayList<>();
        for (Element d : doc.select("div.vrwrap")) {
            Element a = d.selectFirst("h3 a[href]");
            if (a == null) {
                continue;
            }
            Element p = d.selectFirst("p");
            out.add(new WebHit(clean(a.text()), a.attr("abs:href"),
                    p == null ? "" : clean(p.text()), "sogou"));
            if (out.size() >= n) {
                break;
            }
        }
        return out;
    }

    // ---------------- 抓取 ----------------

    public FetchedPage fetch(String url) {
        requireEnabled();
        assertPublicHost(url);
        try {
            Document doc = fetchDocument(url);

            // 先摘掉导航、页脚、脚本这些与正文无关的东西。
            // 不去掉的话正文里会混进大量菜单文字，切分与向量化都被稀释
            doc.select("script, style, noscript, iframe, svg, form, nav, header, "
                    + "footer, aside, .nav, .menu, .sidebar, .comment, .advertisement").remove();

            String title = doc.title().isBlank() ? url : clean(doc.title());

            // 优先取语义化的正文容器；没有就退回 body
            Element main = doc.selectFirst("article");
            if (main == null) {
                main = doc.selectFirst("main");
            }
            if (main == null) {
                main = doc.selectFirst("[role=main]");
            }
            if (main == null) {
                main = doc.selectFirst("#content, .content, #main, .article, .post");
            }
            if (main == null) {
                main = doc.body();
            }
            // 不要用 main.text() —— jsoup 的 .text() 把整棵 DOM 拍平成一串文本，
            // 标题、段落、列表、代码块的结构在这一步全丢。
            // 实测代价：41 篇已入库文档合计只剩 64 个标题行，且绝大多数是这里加的标题，
            // 于是「文档讲了哪几块」在库里无从恢复（想按结构导航就得靠模型猜，而模型
            // 在全局规模上不可靠：整篇定章会返回「整篇一章」）。
            String text = main == null ? "" : toMarkdown(main);

            if (text.length() > props.getMaxTextChars()) {
                text = text.substring(0, props.getMaxTextChars());
            }
            log.debug("抓取 {} → {} 字", url, text.length());
            return new FetchedPage(url, title, text);
        } catch (IOException e) {
            throw new IllegalStateException("抓取失败：" + e.getMessage(), e);
        }
    }

    /** 连续抓取之间歇一下，别把对端当压测目标 */
    public void pauseBetweenFetches() {
        int ms = props.getFetchDelayMs();
        if (ms <= 0) {
            return;
        }
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // ---------------- 内部 ----------------

    private Document fetchDocument(String url) throws IOException {
        return Jsoup.connect(url)
                .userAgent(props.getUserAgent())
                .header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
                .timeout(props.getTimeoutSeconds() * 1000)
                .maxBodySize(props.getMaxPageBytes())
                .followRedirects(true)
                .ignoreContentType(false)   // 非 HTML 的直接失败，别把二进制当文本读
                .get();
    }

    /**
     * 只允许抓公网地址。
     *
     * <p>不加这道检查的话，一条精心构造的搜索结果就能让服务端去请求
     * {@code http://127.0.0.1:6379/} 之类的内网地址 —— 而本机的 Redis
     * 正是监听在 0.0.0.0 上。这是典型的 SSRF。
     *
     * <p>注意要逐个检查 DNS 解析出的<b>全部</b>地址：只查第一个的话，
     * 一个同时解析到公网与内网的域名就能绕过。
     */
    private void assertPublicHost(String url) {
        URI u;
        try {
            u = URI.create(url);
        } catch (Exception e) {
            throw new IllegalArgumentException("网址格式不合法：" + url);
        }
        String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase();
        if (!scheme.equals("http") && !scheme.equals("https")) {
            throw new IllegalArgumentException("只支持 http/https，收到：" + scheme);
        }
        String host = u.getHost();
        if (host == null || host.isBlank()) {
            throw new IllegalArgumentException("网址缺少主机名：" + url);
        }
        InetAddress[] addrs;
        try {
            addrs = InetAddress.getAllByName(host);
        } catch (Exception e) {
            throw new IllegalArgumentException("域名解析失败：" + host);
        }
        for (InetAddress a : addrs) {
            if (a.isLoopbackAddress() || a.isAnyLocalAddress() || a.isLinkLocalAddress()
                    || a.isSiteLocalAddress() || a.isMulticastAddress()
                    || isUniqueLocalV6(a) || isCgnat(a)) {
                throw new IllegalArgumentException(
                        "拒绝抓取内网地址：" + host + " → " + a.getHostAddress());
            }
        }
    }

    private static boolean isUniqueLocalV6(InetAddress a) {
        byte[] b = a.getAddress();
        return b.length == 16 && (b[0] & 0xfe) == 0xfc;   // fc00::/7
    }

    private static boolean isCgnat(InetAddress a) {
        byte[] b = a.getAddress();
        // 100.64.0.0/10 —— 运营商级 NAT，本机所在校园网就在这个段里
        return b.length == 4 && (b[0] & 0xff) == 100 && (b[1] & 0xc0) == 64;
    }

    /** 搜索结果标题里常夹着高亮标记残留与多余空白 */
    private static String clean(String s) {
        return s == null ? "" : s.replaceAll("\\s+", " ").strip();
    }

    /** 合并多余空白：网页正文里的换行与缩进毫无信息量，只会浪费切分预算 */
    private static String normalise(String s) {
        return s.replace(' ', ' ').replaceAll("[ \\t\\x0B\\f\\r]+", " ")
                .replaceAll("\\n{3,}", "\n\n").strip();
    }

    /**
     * 把正文容器转成**保留结构**的 Markdown，而不是拍平成一串文本。
     *
     * <p>保留下来的结构有三处下游收益：
     * <ol>
     *   <li>标题成为独立段落（切分器的 HEADING 规则本来就按标题强制断段），
     *       不会再被并进正文；</li>
     *   <li>空行分隔的段落让切分器按语义边界打包，而不是从长段落里硬切；</li>
     *   <li>「文档讲了哪几块」在库里可恢复 —— 否则要靠模型判章，而它整篇定章时
     *       会直接返回「整篇一章」（逃生口）。</li>
     * </ol>
     *
     * <p>代码块用围栏原样保留：缩进与换行在那里是内容的一部分，压平就废了。
     */
    /**
     * 正文子树 → 自然文档语言。**实现搬到了 {@link com.kniv.ragkb.service.parse.HtmlToText}**，
     * 与上传那条路共用一份 —— 两条路各写一份的代价是：同一份 HTML 走两条路进库会得到两种形状
     * （抓取那条会挑 <main> 剥掉导航，上传那条不会；2026-09-20 实测）。
     */
    private static String toMarkdown(Element root) {
        return com.kniv.ragkb.service.parse.HtmlToText.convert(root);
    }

    private void requireEnabled() {
        if (!props.isEnabled()) {
            throw new IllegalStateException("联网功能未开启（ragkb.web.enabled=false）");
        }
    }
}
