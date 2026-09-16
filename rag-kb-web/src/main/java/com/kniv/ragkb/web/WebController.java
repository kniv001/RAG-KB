package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.service.config.WebProperties;
import com.kniv.ragkb.service.web.WebIngestService;
import com.kniv.ragkb.service.web.WebSearchService;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.*;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 联网搜索与入库。
 *
 * <p>搜索与入库刻意分成两个接口：搜索只返回结果列表让用户先看一眼，
 * 入库才真的抓取并写进知识库。合成一个的话，用户没法在入库前筛掉不想要的来源 ——
 * 而抓进来的每一条都会参与以后的检索，事后清理比事前挑选麻烦得多。
 */
@Slf4j
@RestController
@RequestMapping("/api/web")
@RequiredArgsConstructor
public class WebController {

    private final WebProperties props;
    private final WebSearchService search;
    private final WebIngestService ingest;

    /** 前端据此决定要不要显示联网入口 */
    @GetMapping("/status")
    public R<Map<String, Object>> status() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", props.isEnabled());
        m.put("backends", props.getBackends());
        m.put("maxResults", props.getMaxResults());
        m.put("maxTextChars", props.getMaxTextChars());
        return R.ok(m);
    }

    @PostMapping("/search")
    public R<Map<String, Object>> search(@RequestBody SearchBody body) {
        if (!props.isEnabled()) {
            return R.fail(R.CODE_FORBIDDEN, "联网功能未开启（ragkb.web.enabled=false）");
        }
        if (body == null || body.getQuery() == null || body.getQuery().isBlank()) {
            return R.fail(R.CODE_BAD_REQUEST, "缺少查询词");
        }
        List<WebSearchService.WebHit> hits = search.search(body.getQuery(), body.getCount());

        List<Map<String, Object>> rows = new ArrayList<>(hits.size());
        for (WebSearchService.WebHit h : hits) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("title", h.title());
            m.put("url", h.url());
            m.put("snippet", h.snippet());
            rows.add(m);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("query", body.getQuery());
        out.put("count", rows.size());
        out.put("results", rows);
        return R.ok(out);
    }

    /**
     * 抓取并入库。立即返回（每篇一个索引任务 id），前端照常轮询任务。
     *
     * <p>抓取本身是同步的（要拿到正文才知道成功与否），但向量化那一步是异步的 ——
     * 一篇长文解析加向量化很容易超过 Cloudflare 的 100 秒源站超时。
     */
    @PostMapping("/ingest")
    public R<Map<String, Object>> ingest(@RequestBody IngestBody body) {
        if (!props.isEnabled()) {
            return R.fail(R.CODE_FORBIDDEN, "联网功能未开启（ragkb.web.enabled=false）");
        }
        if (body == null || body.getUrls() == null || body.getUrls().isEmpty()) {
            return R.fail(R.CODE_BAD_REQUEST, "没有要入库的网址");
        }
        if (body.getUrls().size() > props.getMaxResults()) {
            return R.fail(R.CODE_BAD_REQUEST,
                    "一次最多入库 " + props.getMaxResults() + " 篇");
        }

        List<WebIngestService.Ingested> done = ingest.ingest(body.getUrls());
        List<Map<String, Object>> rows = new ArrayList<>(done.size());
        int ok = 0;
        for (WebIngestService.Ingested g : done) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("url", g.url());
            m.put("title", g.title());
            m.put("docId", g.docId());
            m.put("chars", g.chars());
            m.put("taskId", g.taskId());
            m.put("error", g.error());
            rows.add(m);
            if (g.ok()) {
                ok++;
            }
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("requested", body.getUrls().size());
        out.put("succeeded", ok);
        out.put("results", rows);
        return R.ok(out);
    }

    @Data
    public static class SearchBody {
        private String query;
        /** 想要几条结果；为空用配置默认值 */
        private Integer count;
    }

    @Data
    public static class IngestBody {
        private List<String> urls;
    }
}
