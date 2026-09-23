package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.service.config.WebProperties;
import com.kniv.ragkb.service.config.FeedProperties;
import com.kniv.ragkb.service.feed.FeedCrawler;
import com.kniv.ragkb.service.feed.FeedEnrichService;
import com.kniv.ragkb.service.feed.FeedIngestService;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 信息流（{@code news} 分支）的入口。
 *
 * <p>目前只有两个动作：**喂一批网址**、**看读数**。抓取的调度（周期性、按源清单增量）
 * 与"转成文档进知识库"都还没做 —— 先把数据喂起来拿到分布，再决定阈值与调度。
 *
 * <p>遵循与联网搜索同一条姿态：**服务端主动向外发请求的功能默认不开**
 * （{@code ragkb.web.enabled=true} 才开），抓取本身仍走那套 SSRF 防护。
 */
@Slf4j
@RestController
@RequestMapping("/api/feed")
@RequiredArgsConstructor
public class FeedController {

    private final FeedCrawler crawler;
    private final FeedIngestService ingest;
    private final FeedEnrichService enrich;
    private final FeedProperties feedProps;
    private final WebProperties webProps;

    /** 读数的形状见 {@link FeedIngestService.Stats}。 */
    @GetMapping("/stats")
    public R<Map<String, Object>> stats() {
        FeedIngestService.Stats s = ingest.stats();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("总条目", s.total());
        m.put("首见", s.unique());
        m.put("近重复", s.dup());
        m.put("近重复率", String.format("%.1f%%", 100 * s.dupRate()));
        m.put("可召回", s.recallable());
        m.put("冷存", s.cold());
        m.put("来源数", s.sources());
        return R.ok(m);
    }

    /**
     * **富化一批**（第 1、2 层）：粗糙切分 → 段落级"库里有没有" → 议题归并。
     *
     * <p>显式触发而不是入队时自动做：它要 GPU（嵌入），而本机只有一个推理槽 ——
     * 放进抓取请求里会让"抓 10 个网址"从 6 秒变成几十秒，并且直接和问答抢。
     * 由后台在静默窗口里跑（内部走 {@code GpuGate}，有用户请求在跑就直接放弃这一轮）。
     */
    @PostMapping("/enrich")
    public R<Map<String, Object>> enrich(@RequestBody(required = false) EnrichBody body) {
        int limit = body != null && body.getLimit() != null ? body.getLimit() : feedProps.getEnrichLimit();
        FeedEnrichService.Batch b = enrich.run(limit);
        List<Map<String, Object>> rows = new ArrayList<>();
        for (FeedEnrichService.Enriched e : b.items()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", e.id());
            m.put("段数", e.segN());
            m.put("重复段", e.dupSegN());
            m.put("重复率", String.format("%.0f%%", 100 * e.dupRatio()));
            m.put("议题", e.topicId());
            m.put("议题相似", e.topicSim() == null ? null : String.format("%.3f", e.topicSim()));
            m.put("新议题", e.newTopic());
            if (e.note() != null) {
                m.put("说明", e.note());
            }
            rows.add(m);
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("待处理", b.pending());
        res.put("已处理", b.done());
        res.put("新建议题", b.newTopics());
        res.put("判为高度重复", b.demoted());
        if (b.note() != null) {
            res.put("说明", b.note());
        }
        res.put("明细", rows);
        return R.ok(res);
    }

    /** 议题榜（按条目数）——"哪件事在升温"最粗的一个视图。 */
    @GetMapping("/topics")
    public R<List<Map<String, Object>>> topics() {
        return R.ok(enrich.topTopics(20));
    }

    /** 抓一批网址进信息流。 */
    @PostMapping("/urls")
    public R<Map<String, Object>> urls(@RequestBody UrlsBody body) {
        if (!webProps.isEnabled()) {
            return R.fail(R.CODE_FORBIDDEN, "联网功能未开启（ragkb.web.enabled=false）—— 抓取走的是同一套出口");
        }
        if (body == null || body.getUrls() == null || body.getUrls().isEmpty()) {
            return R.fail(R.CODE_BAD_REQUEST, "缺少 urls");
        }
        List<FeedCrawler.Crawled> rows = crawler.crawl(body.getUrls());
        List<Map<String, Object>> out = new ArrayList<>(rows.size());
        for (FeedCrawler.Crawled c : rows) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("url", c.url());
            m.put("title", c.title());
            m.put("chars", c.chars());
            m.put("itemId", c.itemId());
            m.put("duplicateOf", c.dupOf());
            m.put("error", c.error());
            out.add(m);
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("count", out.size());
        res.put("ok", rows.stream().filter(FeedCrawler.Crawled::ok).count());
        res.put("items", out);
        return R.ok(res);
    }

    @Data
    public static class UrlsBody {
        private List<String> urls;
    }

    @Data
    public static class EnrichBody {
        private Integer limit;
    }
}
