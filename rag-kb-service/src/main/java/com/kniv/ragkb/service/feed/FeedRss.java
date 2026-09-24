package com.kniv.ragkb.service.feed;

import lombok.extern.slf4j.Slf4j;
import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import org.jsoup.parser.Parser;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * **RSS / Atom 的解析**：从 feed 里取出「标题 + 链接 + 发布时间」。
 *
 * <p>用 jsoup 的 **XML 解析器**而不是引一个新依赖：项目本来就有 jsoup（抓网页用的），
 * 而 feed 的 XML 结构简单到不值得为它加一个库 —— 多一个依赖就多一处要跟着升级、
 * 也可能在解析上引入自己的失败模式。
 *
 * <p>⚠️ **RSS 与 Atom 是两套标签**（{@code item/link/pubDate} vs {@code entry/link[href]/updated}），
 * 两种都认：中文新闻源现在几乎都是 RSS 2.0，但混进一个 Atom 源就整条通道哑掉，
 * 而那种失败看起来像"这个源没有新闻"。
 */
@Slf4j
public final class FeedRss {

    /** feed 里的一条：**只取这三样** —— 正文要另外抓（feed 只给摘要，甚至只给标题）。 */
    public record Entry(String title, String link, Instant publishedAt) {
    }

    private static final DateTimeFormatter RFC1123 =
            DateTimeFormatter.ofPattern("EEE, dd MMM yyyy HH:mm:ss Z", Locale.ENGLISH);
    private static final DateTimeFormatter SIMPLE =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss", Locale.ENGLISH);

    private FeedRss() {
    }

    public static List<Entry> parse(String xml) {
        List<Entry> out = new ArrayList<>();
        if (xml == null || xml.isBlank()) {
            return out;
        }
        Document doc = Jsoup.parse(xml, "", Parser.xmlParser());
        // RSS 2.0 / RDF
        for (Element item : doc.select("item")) {
            add(out, text(item, "title"), text(item, "link"), text(item, "pubDate"));
        }
        // Atom
        for (Element e : doc.select("entry")) {
            Element link = e.selectFirst("link[href]");
            String href = link == null ? e.selectFirst("link") == null
                    ? null : e.selectFirst("link").text() : link.attr("href");
            String when = e.selectFirst("updated") != null ? e.selectFirst("updated").text()
                    : e.selectFirst("published") != null ? e.selectFirst("published").text() : null;
            add(out, text(e, "title"), href, when);
        }
        return out;
    }

    private static void add(List<Entry> out, String title, String link, String when) {
        if (link == null || link.isBlank()) {
            return;   // 没有链接就没法抓正文，这条直接不算
        }
        out.add(new Entry(title == null ? "" : title.strip(), link.strip(), parseTime(when)));
    }

    private static String text(Element e, String tag) {
        Element x = e.selectFirst(tag);
        return x == null ? null : x.text();
    }

    /**
     * 解析时间。
     *
     * <p>认三种：RFC1123（RSS 标准形）、{@code yyyy-MM-dd HH:mm:ss}（人民网那种）、
     * 以及带 {@code T} 的 ISO 形（Atom）。
     * **解析不出来就返回 null，不猜** —— 拿抓取时间冒充发布时间，
     * "当时我们以为是什么"就永远答不出来了。
     */
    static Instant parseTime(String s) {
        if (s == null || s.isBlank()) {
            return null;
        }
        String t = s.strip();
        try {
            return ZonedDateTime.parse(t, RFC1123).toInstant();
        } catch (Exception ignore) {
            // 继续往下试
        }
        try {
            return OffsetDateTime.parse(t).toInstant();
        } catch (Exception ignore) {
            // 继续往下试
        }
        try {
            return LocalDateTime.parse(t, SIMPLE).atZone(ZoneId.systemDefault()).toInstant();
        } catch (Exception ignore) {
            // 继续往下试
        }
        try {
            // **只到天**（gov.cn 的 pushinfo 就是这种）。补到当天 00:00 ——
            // 宁可粗糙也不能丢：丢了这条的时间维度就整个没有了。
            return java.time.LocalDate.parse(t).atStartOfDay(ZoneId.systemDefault()).toInstant();
        } catch (Exception ignore) {
            // 继续往下试
        }
        log.debug("时间解析不出来：{}", t);
        return null;
    }
}
