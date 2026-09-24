package com.kniv.ragkb.service.feed;

import com.kniv.ragkb.service.web.WebSearchService;
import com.kniv.ragkb.service.web.WebSearchService.FetchedPage;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/**
 * **把一批网址抓下来喂进信息流**（{@link FeedIngestService}）。
 *
 * <p>抓取这一步**直接复用联网搜索那一套**（{@code WebSearchService.fetch}）：
 * SSRF 防护（逐个检查 DNS 解析出的**全部**地址）、抓取间隔、DOM→Markdown 的结构保留，
 * 三样都是现成的，重写一份只会重犯它已经解决过的错。
 *
 * <p>与 {@code WebIngestService} 的分工：那个是"抓一页 → **直接进知识库**"（走 chunks/向量），
 * 这个是"抓一批 → **先进信息流**"（过闸、去重、留痕），值得进知识库的由后续步骤转过去。
 * 两条路的差别只在**要不要先过闸**：信息流方向的量级大得多，不能抓来什么就索引什么。
 *
 * <p>刻意逐个抓、不并行：抓取之间有间隔（礼貌 + 反爬），而且向量化那一步本来就在抢同一块 GPU。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class FeedCrawler {

    private final WebSearchService search;
    private final FeedIngestService ingest;

    /** 一条抓取结果。抓失败不抛，落进 {@code error} —— 一批里坏一条不该中断整批。 */
    public record Crawled(String url, String title, int chars, Long itemId,
                          Integer status, Long dupOf, String error) {
        public boolean ok() {
            return error == null;
        }
    }

    public List<Crawled> crawl(List<String> urls) {
        List<Crawled> out = new ArrayList<>();
        for (int i = 0; i < urls.size(); i++) {
            if (i > 0) {
                search.pauseBetweenFetches();
            }
            out.add(crawlOne(urls.get(i), null));
        }
        return out;
    }

    /**
     * 抓一条并入库。
     *
     * @param publishedAt feed 里给出的发布时间（RSS 的 pubDate）。
     *                    **从 feed 来的就一定要带上** —— 网页正文里多半抽不到发布时间，
     *                    而"什么时候发生的"正是这一支最要紧的那一维；
     *                    抽不到时才留 null，**绝不拿抓取时间冒充**。
     */
    public Crawled crawlOne(String url, Instant publishedAt) {
        try {
            FetchedPage page = search.fetch(url);
            FeedIngestService.Ingested r =
                    ingest.ingest(page.url(), page.title(), page.text(), publishedAt);
            return new Crawled(page.url(), page.title(), page.text().length(),
                    r.id(), r.status(), r.dupOf(), null);
        } catch (Exception e) {
            log.warn("信息流抓取失败 {}：{}", url, e.getMessage());
            return new Crawled(url, null, 0, null, null, null, e.getMessage());
        }
    }
}
