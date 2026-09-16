package com.kniv.ragkb.service.web;

import com.kniv.ragkb.dao.mapper.DocumentMapper;
import com.kniv.ragkb.domain.entity.Document;
import com.kniv.ragkb.service.config.StorageProperties;
import com.kniv.ragkb.service.index.IndexTaskService;
import com.kniv.ragkb.service.web.WebSearchService.FetchedPage;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * 把联网抓到的网页收进知识库。
 *
 * <p><b>为什么走「入库」而不是「让 agent 直接联网找答案」</b>：
 * 整个 agent 的设计有一条硬约束 —— 可用动作只有「检索」，事实来源始终只有
 * 检索到的资料，不接外部工具、不联网。这条约束保证了回答的可追溯性
 * （每条结论都能指回某个文档的某一块）。
 *
 * <p>联网如果做成 agent 的一个动作，那条约束当场就破了：模型可以拿一份
 * 没进过库、没被审过、也不知道出处的网页内容直接作答，而用户完全看不出来。
 * 所以这里的做法是让网页内容<b>变成文档</b>再进检索 —— 与用户自己上传的文件
 * 走同一条路，一样会被切分、向量化、留下出处。
 *
 * <p>代价是慢（抓取 + 解析 + 向量化），收益是「回答里的每一句都有出处」这条
 * 性质在联网之后依然成立。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class WebIngestService {

    /** 正文短于这个长度就判定为抓取失败 —— 多半是 JS 渲染的页面或反爬页 */
    private static final int MIN_USEFUL_CHARS = 200;

    private final WebSearchService search;
    private final DocumentMapper documents;
    private final StorageProperties storage;
    private final IndexTaskService tasks;

    /** 一篇入库结果 */
    public record Ingested(String url, String title, String docId, Integer chars,
                           String taskId, String error) {
        public boolean ok() {
            return error == null;
        }
    }

    /**
     * 抓取并入库一批网址。逐个处理，单个失败不影响其余。
     *
     * <p>刻意不做成并行：抓取之间有间隔（见 {@code fetchDelayMs}），
     * 而且向量化那一步本来就在抢同一块 GPU。
     */
    public List<Ingested> ingest(List<String> urls) {
        List<Ingested> out = new ArrayList<>();
        for (int i = 0; i < urls.size(); i++) {
            String url = urls.get(i);
            if (i > 0) {
                search.pauseBetweenFetches();
            }
            try {
                out.add(one(url));
            } catch (Exception e) {
                log.warn("入库失败 {}：{}", url, e.getMessage());
                out.add(new Ingested(url, null, null, null, null, e.getMessage()));
            }
        }
        return out;
    }

    private Ingested one(String url) throws IOException {
        FetchedPage page = search.fetch(url);
        if (page.text().length() < MIN_USEFUL_CHARS) {
            throw new IllegalStateException("正文只有 " + page.text().length()
                    + " 字，可能是需要 JS 渲染的页面或反爬页");
        }

        String docId = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        String name = safeName(page.title(), docId);
        Path dest = storage.uploads().resolve(docId + ".md");

        // 把出处写在正文最前面：文件本身要能自证来历，
        // 单独看这个 .md 时也能知道它是哪来的、什么时候抓的
        OffsetDateTime now = OffsetDateTime.now();
        String body = "# " + page.title() + "\n\n"
                + "> 来源：" + page.url() + "\n"
                + "> 抓取时间：" + now.toLocalDate() + "\n"
                + "> 由联网搜索抓取入库，网页内容可能随时间变化。\n\n"
                + page.text() + "\n";
        Files.writeString(dest, body, StandardCharsets.UTF_8);

        Document doc = new Document();
        doc.setId(docId);
        doc.setName(name);
        doc.setSuffix(".md");
        doc.setBytes((long) body.getBytes(StandardCharsets.UTF_8).length);
        doc.setStoredPath(storage.getRoot().toAbsolutePath().normalize()
                .relativize(dest).toString().replace('\\', '/'));
        doc.setStatus("stored");
        doc.setChunkCount(0);
        doc.setEmbedModel("");
        doc.setSourceKind("web");
        doc.setSourceUrl(page.url());
        doc.setFetchedAt(now);
        documents.insert(doc);

        String taskId = tasks.submit(docId, storage.getRoot());
        log.info("联网入库：{} → {}（{} 字）", page.url(), docId, page.text().length());
        return new Ingested(page.url(), page.title(), docId, page.text().length(), taskId, null);
    }

    /** 网页标题常含路径分隔符与超长串，落盘前得收拾干净 */
    private static String safeName(String title, String fallbackId) {
        String t = title == null ? "" : title.strip()
                .replaceAll("[\\\\/:*?\"<>|\\r\\n]", " ")
                .replaceAll("\\s+", " ")
                .strip();
        if (t.isBlank()) {
            t = "网页抓取-" + fallbackId;
        }
        if (t.length() > 60) {
            t = t.substring(0, 60);
        }
        return t + ".md";
    }
}
