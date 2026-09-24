package com.kniv.ragkb.service.feed;

import com.kniv.ragkb.dao.mapper.DocumentMapper;
import com.kniv.ragkb.dao.mapper.FeedMapper;
import com.kniv.ragkb.domain.entity.Document;
import com.kniv.ragkb.service.config.FeedProperties;
import com.kniv.ragkb.service.config.StorageProperties;
import com.kniv.ragkb.service.index.IndexTaskService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * **晋升**：把信息流条目转成知识库文档，让它们**能被问答检索到**。
 *
 * <p>在这之前整条信息流是个**孤岛** —— 抓了一堆、算了重复率与议题，
 * 而问"最近金价怎么样"时 agent 一个字都看不到。这一步就是打通它。
 *
 * <p><b>为什么不是"全都晋升"</b>：母项目的知识库是**用户自己的 44 篇文档**，
 * 而问答只取 top-k 段。一次几百条新闻灌进去，每条都会去争那几个名额 ——
 * 检索结果会被新闻挤掉原本的资料，而这是**立刻发生**的、不可逆影响（子台账里
 * "入库但不召回"那条原则管的是**存储**，这里是**召回池**，两件事）。
 * 所以第一版只晋升**够格**的，规则**显式且可调**：
 *
 * <ul>
 *   <li>{@code status = 1}（可召回，即过了闸 0）；</li>
 *   <li>{@code dup_of IS NULL} —— 近重复的**转载**不晋升（它没有新信息，
 *       只会让同一个事件的段落成倍地占名额）；</li>
 *   <li>**议题里至少 {@code minTopicItems} 条** —— 也就是"有多家（或多个栏目）
 *       报道过这件事"。这是在没有模型权重（闸 1/2 还没做）之前，
 *       唯一一个**不靠猜**的"值得进库"信号：**孤零零一条新闻，很可能就是一条公关稿或花絮**。</li>
 * </ul>
 *
 * <p>⚠️ **已晋升的不会重复晋升**（{@code doc_id IS NULL} 是前置条件），
 * 且晋升只是**多写一份文档**，`feed_items` 那一行一个字都不改（append-only），
 * 只在 {@code doc_id} 上记下它变成了哪篇文档。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class FeedPromoteService {

    private final FeedMapper feed;
    private final DocumentMapper documents;
    private final StorageProperties storage;
    private final IndexTaskService tasks;
    private final FeedProperties props;

    /** 一次晋升的结果。{@code skipped} 与 {@code promoted} 都要报出来 ——
     *  只报晋升数的话，"规则太严一条没选上"与"已经没有可晋升的"看起来一样。 */
    public record Result(int candidates, int promoted, List<String> docIds, String note) {
    }

    public Result promote(int limit, int minTopicItems, boolean dryRun) {
        List<Map<String, Object>> rows = feed.promotable(limit, minTopicItems);
        if (rows.isEmpty()) {
            return new Result(0, 0, List.of(), "没有够格的条目（可召回 + 非转载 + 议题至少 "
                    + minTopicItems + " 条）");
        }
        if (dryRun) {
            List<String> names = new ArrayList<>();
            for (Map<String, Object> r : rows) {
                names.add(FeedValues.str(r.get("title")));
            }
            return new Result(rows.size(), 0, names, "试跑：只列出会晋升哪些，改动一个都没做");
        }

        List<String> docIds = new ArrayList<>();
        for (Map<String, Object> r : rows) {
            long itemId = ((Number) r.get("id")).longValue();
            try {
                // ⚠️ **时间列别强转**：timestamptz 取出来是 java.sql.Timestamp，
                // 强转 OffsetDateTime 会 ClassCastException（第一版就是这么八条全失败的）。
                // 取值一律走 FeedValues —— 这一支已经被"从 map 取值"咬了三次。
                docIds.add(one(itemId, FeedValues.str(r.get("title")), FeedValues.str(r.get("body")),
                        FeedValues.str(r.get("url")), FeedValues.time(r.get("publishedAt"))));
            } catch (Exception e) {
                log.warn("晋升失败 #{}：{}", itemId, e.getMessage());
            }
        }
        log.info("信息流晋升：候选 {} 条 → 成功 {} 条", rows.size(), docIds.size());
        return new Result(rows.size(), docIds.size(), docIds, null);
    }

    private String one(long itemId, String title, String body, String url,
                       OffsetDateTime publishedAt) throws Exception {
        String docId = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        String name = safeName(title, docId);
        Path dest = storage.uploads().resolve(docId + ".md");

        // 出处写在最前面：文件本身要能自证来历（与联网入库同一条规矩）。
        // **发布时间与抓取时间都写** —— 这一支的时间语义是"补充/推翻"，
        // 只留一个时间就答不出"当时我们以为是什么"。
        OffsetDateTime now = OffsetDateTime.now();
        String text = "# " + title + "\n\n"
                + "> 来源：" + url + "\n"
                + (publishedAt == null ? "" : "> 发布时间：" + publishedAt.toLocalDate() + "\n")
                + "> 抓取时间：" + now.toLocalDate() + "\n"
                + "> 由信息流（新闻抓取）晋升入库。\n\n"
                + body + "\n";
        Files.writeString(dest, text, StandardCharsets.UTF_8);

        Document doc = new Document();
        doc.setId(docId);
        doc.setName(name);
        doc.setSuffix(".md");
        doc.setBytes((long) text.getBytes(StandardCharsets.UTF_8).length);
        doc.setStoredPath(storage.getRoot().toAbsolutePath().normalize()
                .relativize(dest).toString().replace('\\', '/'));
        doc.setStatus("stored");
        doc.setChunkCount(0);
        doc.setEmbedModel("");
        // **source_kind 用 'feed' 而不是 'web'**：两者都是抓来的，但这一条要能单独筛出来 ——
        // "把新闻都撤掉"应该是**一条 SQL**，而不是靠名字去猜哪些是新闻。
        doc.setSourceKind("feed");
        doc.setSourceUrl(url);
        doc.setFetchedAt(now);
        documents.insert(doc);

        // 切分 + 向量化走**现成的**索引任务（后台线程池 + GpuGate），不另写一份
        tasks.submit(docId, storage.getRoot());
        feed.markPromoted(itemId, docId);
        log.info("晋升：条目 #{} → 文档 {}（{}）", itemId, docId, shorten(title));
        return docId;
    }

    /** 标题里常带路径分隔符与超长串，落盘前收拾干净（与联网入库同一套）。 */
    private static String safeName(String title, String fallbackId) {
        String t = title == null ? "" : title.strip()
                .replaceAll("[\\\\/:*?\"<>|\\r\\n]", " ")
                .replaceAll("\\s+", " ")
                .strip();
        if (t.isBlank()) {
            t = "信息流-" + fallbackId;
        }
        if (t.length() > 60) {
            t = t.substring(0, 60);
        }
        return t + ".md";
    }

    private static String shorten(String s) {
        if (s == null) {
            return "(无标题)";
        }
        return s.length() <= 30 ? s : s.substring(0, 30) + "…";
    }
}
