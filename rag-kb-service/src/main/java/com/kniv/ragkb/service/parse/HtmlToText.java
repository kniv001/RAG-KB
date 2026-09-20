package com.kniv.ragkb.service.parse;

import org.jsoup.Jsoup;
import org.jsoup.nodes.Element;
import org.jsoup.select.Elements;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * HTML → **自然的文档语言**（上传与抓取两条路共用这一份实现）。
 *
 * <p>为什么需要它（2026-09-20 实测，用一张合成靶子页走真实入库路径）：
 * <ol>
 *   <li><b>导航与页脚都进来了</b> —— 原来的 {@code fromHtml} 是正则剥标签，
 *       只删 script/style，{@code <nav>}/{@code <footer>} 的**文本全留下**；
 *   <li><b>标题层级完全没有</b> —— {@code <h1>} 与正文无区别，于是「标题路径随块携带」
 *       那条（免费且比语境行保真的 contextual embedding）**无从下手**；
 *   <li><b>表格的列义只由位置隐含</b> —— 行被拍平成「计数器 不支持 低 内部接口兜底」，
 *       表头与行**没有任何绑定**，块一切开就丢列义。
 * </ol>
 *
 * <p>修法全是机械的（不需要模型）：
 * <ul>
 *   <li>按标签剥 {@code nav/footer/aside/form/...} 以及 {@code role=navigation|banner|contentinfo}；
 *   <li>标题输出 Markdown 前缀，给「标题路径」留出可抽取的结构；
 *   <li><b>表格线性化</b>：把列头**绑进每一行**，一行写成一整句 ——
 *       切在哪儿都不丢列义：
 *       <pre>计数器：突发流量="不支持"；实现复杂度="低"；典型场景="内部接口兜底"</pre>
 *       表格的语义在**表头↔单元格的关系**里，而扁平化恰好把那个关系丢掉了。
 * </ul>
 *
 * <p>代码块用围栏原样保留：缩进与换行在那里是内容的一部分，压平就废了。
 */
public final class HtmlToText {

    /** 噪声标签：整棵子树丢掉（含文本）。 */
    private static final Set<String> NOISE_TAGS = Set.of(
            "script", "style", "noscript", "template", "iframe", "svg", "canvas",
            "nav", "footer", "aside", "form", "button", "input", "select", "textarea");

    /** 这些角色按语义就是"页面外壳"，标签名不一定叫 nav/footer。 */
    private static final String NOISE_ROLES =
            "[role=navigation],[role=banner],[role=contentinfo],[role=search],[aria-hidden=true]";

    private HtmlToText() {
    }

    /** 整页 HTML → 文本。 */
    public static String convert(String html) {
        if (html == null || html.isBlank()) {
            return "";
        }
        return convert(Jsoup.parse(html).body());
    }

    /** 已选好正文子树（抓取那条路先挑 {@code <main>}）→ 文本。 */
    public static String convert(Element root) {
        if (root == null) {
            return "";
        }
        Element clean = root.clone();
        clean.select(String.join(",", NOISE_TAGS)).remove();
        clean.select(NOISE_ROLES).remove();
        StringBuilder sb = new StringBuilder();
        walk(clean, sb);
        return sb.toString()
                .replace(' ', ' ')
                .replace('　', ' ')
                .replaceAll("[ \\t\\x0B\\f]+\\n", "\n")   // 行尾空白
                .replaceAll("[ \\t]{2,}", " ")
                .replaceAll("\\n{3,}", "\n\n")            // 连续空行压成一个
                .strip();
    }

    private static void walk(org.jsoup.nodes.Node node, StringBuilder sb) {
        if (node instanceof org.jsoup.nodes.TextNode t) {
            sb.append(t.getWholeText());
            return;
        }
        if (!(node instanceof Element el)) {
            return;
        }
        String tag = el.tagName().toLowerCase();
        switch (tag) {
            case "h1", "h2", "h3", "h4", "h5", "h6" -> {
                int level = tag.charAt(1) - '0';
                sb.append("\n\n").append("#".repeat(level)).append(' ');
                el.childNodes().forEach(c -> walk(c, sb));
                sb.append("\n\n");
            }
            case "p", "div", "section", "article", "blockquote", "figure", "figcaption",
                 "ul", "ol", "dl", "dt", "dd" -> {
                sb.append("\n\n");
                el.childNodes().forEach(c -> walk(c, sb));
                sb.append("\n\n");
            }
            case "li" -> {
                sb.append("\n- ");
                el.childNodes().forEach(c -> walk(c, sb));
            }
            case "table" -> appendTable(el, sb);
            case "tr", "thead", "tbody", "tfoot", "td", "th" ->
                    // 表格外的散装 td/th（有些页面不用 table 排）按普通块处理
                    el.childNodes().forEach(c -> walk(c, sb));
            case "br" -> sb.append('\n');
            case "hr" -> sb.append("\n\n---\n\n");
            case "pre" -> sb.append("\n\n```\n").append(el.wholeText().strip()).append("\n```\n\n");
            case "code" -> {
                if (el.parent() != null && "pre".equals(el.parent().tagName())) {
                    el.childNodes().forEach(c -> walk(c, sb));       // pre 里已经加了围栏
                } else {
                    sb.append('`').append(el.wholeText().strip()).append('`');
                }
            }
            default -> el.childNodes().forEach(c -> walk(c, sb));
        }
    }

    /**
     * 表格线性化：**列头绑进每一行**，一行一整句。
     *
     * <p>为什么不用 Markdown 竖线表：竖线表把列义仍留在"位置"上 ——
     * 切块器在表格中间切一刀，后半张表的每一格就都不知道自己是什么列了。
     * 线性化之后**每一行自带列名**，切在哪儿都不丢。
     */
    private static void appendTable(Element table, StringBuilder sb) {
        List<String> headers = new ArrayList<>();
        Element headRow = table.selectFirst("thead tr");
        if (headRow == null) {
            headRow = table.selectFirst("tr");
        }
        if (headRow != null) {
            for (Element c : headRow.select("th,td")) {
                headers.add(c.text().strip());
            }
        }

        Element caption = table.selectFirst("caption");
        sb.append("\n\n");
        if (caption != null && !caption.text().isBlank()) {
            sb.append("**").append(caption.text().strip()).append("**\n");
        }

        Elements bodyRows = table.select("tbody tr");
        if (bodyRows.isEmpty()) {
            bodyRows = table.select("tr");
            if (headRow != null) {
                bodyRows.remove(headRow);      // 表头那一行已经变成列名了，不再当数据行
            }
        }
        int n = 0;
        for (Element row : bodyRows) {
            Elements cells = row.select("th,td");
            if (cells.isEmpty()) {
                continue;
            }
            StringBuilder line = new StringBuilder();
            for (int i = 0; i < cells.size(); i++) {
                String v = cells.get(i).text().strip();
                if (v.isEmpty()) {
                    continue;
                }
                String label = i < headers.size() && !headers.get(i).isEmpty()
                        ? headers.get(i) : ("列" + (i + 1));
                if (line.length() > 0) {
                    line.append("；");
                }
                line.append(label).append('：').append(v);
            }
            if (line.length() > 0) {
                sb.append("- ").append(line).append('\n');
                n++;
            }
        }
        if (n == 0) {
            // 空表（只有表头）：至少把表头留下，别静默丢
            if (!headers.isEmpty()) {
                sb.append("- ").append(String.join("；", headers)).append('\n');
            }
        }
        sb.append("\n\n");
    }
}
