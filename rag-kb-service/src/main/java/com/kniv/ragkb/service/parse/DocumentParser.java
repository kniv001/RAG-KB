package com.kniv.ragkb.service.parse;

import lombok.extern.slf4j.Slf4j;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.apache.poi.xwpf.extractor.XWPFWordExtractor;
import org.apache.poi.xwpf.usermodel.XWPFDocument;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/**
 * 文档解析：文件 → 纯文本。
 *
 * <p>与 Python 版对齐的策略：未知后缀按纯文本处理而不是报错 ——
 * 用户上传 .log / .conf 这类纯文本时不该被拦住。
 *
 * <p>PDF 用 PDFBox、DOCX 用 POI。两者都是重量级依赖，所以只在真正遇到对应格式时
 * 才触发加载，普通文本走不到它们。
 */
@Slf4j
@Component
public class DocumentParser {

    private static final Pattern BLANK_RUN = Pattern.compile("\n{3,}");

    /**
     * **解析器版本** —— 改了解析逻辑就把它加一。
     *
     * <p>解析结果按「文件内容哈希 + 本版本」缓存在库里，只按内容哈希的话，
     * 换了解析器之后重新索引**仍会拿到旧解析**（2026-09-20 改 HTML 转换器时踩过：
     * 新代码确实在运行的 jar 里，入库形状却是旧的；查了半天才发现是缓存命中）。
     *
     * <p>版本史：{@code v2} = HTML 走 {@link HtmlToText}（剥导航页脚 / 标题层级 / 表格线性化）。
     */
    public static final String VERSION = "v2";

    public static class ParseException extends RuntimeException {
        public ParseException(String message) {
            super(message);
        }

        public ParseException(String message, Throwable cause) {
            super(message, cause);
        }
    }

    public String parse(Path path) {
        String name = path.getFileName().toString().toLowerCase();
        try {
            if (name.endsWith(".pdf")) {
                return clean(fromPdf(path));
            }
            if (name.endsWith(".docx")) {
                return clean(fromDocx(path));
            }
            String raw = Files.readString(path, StandardCharsets.UTF_8);
            if (name.endsWith(".html") || name.endsWith(".htm")) {
                return clean(fromHtml(raw));
            }
            if (name.endsWith(".csv")) {
                return clean(fromCsv(raw));
            }
            if (name.endsWith(".json")) {
                return clean(fromJson(raw));
            }
            return clean(raw);
        } catch (IOException e) {
            throw new ParseException("读取失败：" + e.getMessage(), e);
        }
    }

    // ---------------- 各格式 ----------------

    private String fromPdf(Path path) throws IOException {
        try (PDDocument doc = Loader.loadPDF(path.toFile())) {
            if (doc.getNumberOfPages() == 0) {
                return "";
            }
            PDFTextStripper stripper = new PDFTextStripper();
            stripper.setSortByPosition(true);   // 不排序的话分栏排版会串行
            return stripper.getText(doc);
        }
    }

    private String fromDocx(Path path) throws IOException {
        try (InputStream in = Files.newInputStream(path);
             XWPFDocument doc = new XWPFDocument(in);
             XWPFWordExtractor extractor = new XWPFWordExtractor(doc)) {
            return extractor.getText();
        }
    }

    /**
     * HTML → 自然文档语言。**别再改回正则剥标签**：那样会留下 nav/footer 的文本、
     * 丢掉标题层级、把表格拍平成"列义只由位置隐含"（2026-09-20 用合成靶子页实测过三条）。
     * 实现与抓取那条路（{@code WebSearchService}）共用同一份 {@link HtmlToText}。
     */
    private String fromHtml(String raw) {
        return HtmlToText.convert(raw);
    }

    /** CSV 拼成「表头 | 值 | 值」的行文本 —— 保留列语义，切分后模型才看得懂上下文。 */
    private String fromCsv(String raw) {
        List<String> lines = new ArrayList<>();
        for (String line : raw.split("\r?\n")) {
            if (!line.isBlank()) {
                lines.add(String.join(" | ", splitCsvLine(line)));
            }
        }
        return String.join("\n", lines);
    }

    /** 极简 CSV 拆分：支持双引号包裹的字段，但不管引号内的转义逗号（个人知识库够用）。 */
    private static List<String> splitCsvLine(String line) {
        List<String> out = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean quoted = false;
        for (int i = 0; i < line.length(); i++) {
            char ch = line.charAt(i);
            if (ch == '"') {
                quoted = !quoted;
            } else if (ch == ',' && !quoted) {
                out.add(cur.toString().trim());
                cur.setLength(0);
            } else {
                cur.append(ch);
            }
        }
        out.add(cur.toString().trim());
        return out;
    }

    /** JSON 原样格式化输出即可，模型看得懂结构。格式不合法就按纯文本。 */
    private String fromJson(String raw) {
        return raw;   // 不引入额外解析：保留原貌比格式化后丢失信息更安全
    }

    private static String clean(String text) {
        String t = text == null ? "" : text.replace("\r\n", "\n").replace("\r", "\n");
        return BLANK_RUN.matcher(t).replaceAll("\n\n").strip();
    }
}
