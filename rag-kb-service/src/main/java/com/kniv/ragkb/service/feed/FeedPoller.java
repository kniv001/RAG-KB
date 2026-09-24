package com.kniv.ragkb.service.feed;

import com.kniv.ragkb.dao.mapper.FeedMapper;
import com.kniv.ragkb.service.config.FeedProperties;
import com.kniv.ragkb.service.config.WebProperties;
import com.kniv.ragkb.service.web.WebSearchService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

/**
 * **周期抓取**：按**层级顺序**轮询频道（RSS），把新条目抓成信息流条目。
 *
 * <p>三层分工，各自只做一件事：
 * <pre>
 *   feed_channels（哪个接口）──RSS──▶ 标题+链接+pubDate（**不含正文**）
 *                                        │
 *                          只取比 last_item_at 新的
 *                                        ▼
 *   FeedCrawler.crawlOne（抓正文 + 闸 0 + simhash 去重 + 落库）
 *                                        ▼
 *   FeedEnrichService（切段 + 段落回归 + 议题归并）—— 要 GPU，走 GpuGate 让路
 * </pre>
 *
 * <p><b>"分层推进"落在两处</b>：① 频道按 {@code tier} 排序，低层级（国家级=1）先抓；
 * ② 每轮有**总预算**（{@code max-per-run}），预算先给高层级的源 —— 这样即使一轮跑不完，
 * 也不会出现"门户页抓了一堆、国家级的没轮到"。
 *
 * <p>⚠️ 与联网功能共用同一个开关：这东西会让服务端主动向外发请求，
 * 所以 {@code ragkb.web.enabled} 关着一律不动（抓取走的是同一个出口）。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class FeedPoller {

    private final FeedMapper feed;
    private final FeedCrawler crawler;
    private final FeedEnrichService enrich;
    private final WebSearchService search;
    private final FeedProperties props;
    private final WebProperties webProps;

    /** 上一轮跑了多久 / 抓了几条 —— 供 /api/feed/stats 之类的地方看（暂存内存即可）。 */
    private volatile String lastRun = "还没跑过";

    public String lastRun() {
        return lastRun;
    }

    /**
     * 定时轮询。
     *
     * <p>用 {@code fixedDelay}（上一轮结束再计时）而不是 {@code fixedRate}：
     * 一轮里要抓十几个网页、还要等 GPU 静默，时长本来就不固定；
     * 用 fixedRate 会在慢轮里堆积。
     */
    @Scheduled(initialDelayString = "${ragkb.feed.poll-initial-delay-ms:120000}",
            fixedDelayString = "${ragkb.feed.poll-interval-ms:1800000}")
    public void poll() {
        if (!props.isPollEnabled()) {
            return;
        }
        if (!webProps.isEnabled()) {
            lastRun = "没跑：联网功能关着（ragkb.web.enabled=false）";
            return;
        }
        long t0 = System.currentTimeMillis();
        int budget = props.getMaxPerRun();
        int newItems = 0;
        int channels = 0;
        List<String> notes = new ArrayList<>();

        List<Map<String, Object>> chans = feed.pollableChannels(props.getTierMax());
        for (Map<String, Object> ch : chans) {
            if (budget <= 0) {
                break;
            }
            long id = ((Number) ch.get("id")).longValue();
            String url = str(ch.get("url"));
            String label = str(ch.get("label")) + "@" + str(ch.get("domain"));
            try {
                String body = search.fetchRaw(url);
                // 两种通道：RSS（XML）与站点接口（JSON）。**类型与字段映射都在库里**
                // （feed_channels.kind / array_path / f_*），换站点只改数据不改代码。
                List<FeedRss.Entry> entries = "json".equalsIgnoreCase(str(ch.get("kind")))
                        ? FeedJson.parse(body, str(ch.get("arrayPath")), str(ch.get("fTitle")),
                                str(ch.get("fLink")), str(ch.get("fDate")))
                        : FeedRss.parse(body);
                if (entries.isEmpty()) {
                    feed.markChannelError(id, "这个频道一条条目都取不到（RSS 解析失败？映射不对？）");
                    notes.add(label + "：feed 空");
                    continue;
                }
                Instant seen = toInstant(ch.get("lastItemAt"));
                // **按时间升序取**（最旧的先抓），锚点只朝前走。
                //
                // 为什么不是"最新优先"：锚点是"已见过的最新"，而**未处理的永远更旧** ——
                // 一旦一轮里新的条目超过每频道上限，"最新优先 + 锚点跳到最新"会让
                // 那些更旧的条目**永远落在锚点之下、再也抓不到**，而且完全不报错。
                // 升序取则天然不会漏：处理到哪，锚点就走到哪，下一轮从那儿接着走。
                // 代价是突发时先处理稍旧的（而 5 频道 × 10 条/轮 的吞吐，积压几轮就追平）。
                List<FeedRss.Entry> candidates = entries.stream()
                        .filter(e -> seen == null || (e.publishedAt() != null && e.publishedAt().isAfter(seen)))
                        .sorted(Comparator.comparing(FeedRss.Entry::publishedAt,
                                Comparator.nullsLast(Comparator.naturalOrder())))
                        .toList();
                // **抓之前先查 URL 知不知道** —— 一条 HTTP 换一次查库，很划算；
                // 而且这是"取回同一条又抓一遍"的唯一防线（feed 会连着几轮都给出同一个链接）。
                List<FeedRss.Entry> fresh = candidates.stream()
                        .filter(e -> feed.idByUrl(e.link()) == null)
                        .limit(Math.min(budget, props.getPerChannelMax()))
                        .toList();

                int added = 0;
                int skipped = candidates.size() - fresh.size();
                Instant newest = seen;
                for (FeedRss.Entry e : fresh) {
                    if (budget <= 0) {
                        break;
                    }
                    search.pauseBetweenFetches();
                    budget--;
                    FeedCrawler.Crawled c = crawler.crawlOne(e.link(), e.publishedAt());
                    if (c.ok()) {
                        added++;
                        newItems++;
                    }
                    if (e.publishedAt() != null
                            && (newest == null || e.publishedAt().isAfter(newest))) {
                        newest = e.publishedAt();
                    }
                }
                // 升序取 ⇒ 锚点就是"取到的最新一条"，只朝前走，不会漏。
                feed.markChannelFetched(id, newest == null ? null
                        : OffsetDateTime.ofInstant(newest, ZoneId.systemDefault()), added);
                int left = Math.max(0, candidates.size() - skipped - fresh.size());
                channels++;
                notes.add(label + "：" + entries.size() + " 条 · 待抓 " + candidates.size()
                        + "（已知 " + skipped + "、本轮上限外 " + left + "）· 入库 " + added);
            } catch (Exception e) {
                feed.markChannelError(id, shorten(e.getMessage()));
                notes.add(label + "：失败 " + shorten(e.getMessage()));
                log.warn("频道抓取失败 {}：{}", url, e.getMessage());
            }
        }

        // 抓完之后富化（切段 + 议题）。**它要 GPU**，内部会等静默；
        // 有用户请求在跑就直接放弃这一轮，不抢 —— 摘要/轮次笔记都是这个规矩。
        int enriched = 0;
        if (newItems > 0) {
            FeedEnrichService.Batch b = enrich.run(props.getEnrichLimit());
            enriched = b.done();
        }
        long ms = System.currentTimeMillis() - t0;
        lastRun = String.format("%d 个频道、新入库 %d 条、富化 %d 条、耗时 %.1fs | %s",
                channels, newItems, enriched, ms / 1000.0, String.join("；", notes));
        log.info("信息流轮询：{}", lastRun);
    }

    private static Instant toInstant(Object o) {
        if (o == null) {
            return null;
        }
        if (o instanceof OffsetDateTime odt) {
            return odt.toInstant();
        }
        if (o instanceof java.sql.Timestamp ts) {
            return ts.toInstant();
        }
        return null;
    }

    private static String str(Object o) {
        return o == null ? "" : o.toString();
    }

    private static String shorten(String s) {
        if (s == null) {
            return "(无信息)";
        }
        return s.length() <= 60 ? s : s.substring(0, 60) + "…";
    }
}
