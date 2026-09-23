package com.kniv.ragkb.service.feed;

import com.kniv.ragkb.dao.mapper.FeedMapper;
import com.kniv.ragkb.domain.entity.FeedItem;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.List;
import java.util.Locale;

/**
 * **信息流条目的入库**：来源 → 闸 0 → simhash 去重 → 落库。
 *
 * <p>这一步里**没有一次模型调用**（闸 1/2 与实体抽取都还没做），所以它是纯 CPU + DB、
 * 可以随时跑、随时停 —— 先用它把数据喂起来，拿到**近重复率 / 条目-来源比 / 闸的分布**
 * 三个数，再决定第 1、2 层的阈值。**阈值要用数据定，不拍。**
 *
 * <p>三条设计要点，写在代码里因为最容易在下一次修改时被违反：
 * <ol>
 *   <li><b>入库 ≠ 可见</b>：闸 0 判出的是 {@code status}，行一律留下
 *       （丢弃不可逆；校准来源权重需要保留本该丢的样本）；</li>
 *   <li><b>只给"可召回"的条目建 LSH 索引</b>：垃圾页高度自相似，把它们也塞进索引，
 *       会让真正的条目被标成"某导航页的重复"。索引小、且语义干净 —— 重复判定只在
 *       召回池内部发生，而那正是检索唯一在乎的范围；</li>
 *   <li><b>同址重抓不改写历史判断</b>：只更新抓取时间与正文。</li>
 * </ol>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class FeedIngestService {

    private final FeedMapper feed;

    /** 闸 0 的参数。先宽后紧：宁可先冷存，也别在第一版就把数据丢掉。 */
    private final FeedGate.Config gateConfig = FeedGate.Config.defaults();

    /** 一次入库的结果。{@code duplicate} 与 {@code refreshed} 是两种不同的"没新增"，
     *  分开报是因为它们对重复率统计的意义完全相反：前者是**内容重复**（要量的正是它），
     *  后者只是**同一个网址又抓了一次**（与重复率无关，不该混进去）。 */
    public record Ingested(long id, String url, String domain, int status, String reason,
                           Long dupOf, long simhash, boolean duplicate, boolean refreshed) {
    }

    public Ingested ingest(String url, String title, String body, Instant publishedAt) {
        String domain = domainOf(url);
        Long sourceId = ensureSource(domain);
        int charN = body == null ? 0 : body.length();

        // ① 同址重抓：不新增行、不重判可见性
        Long existing = feed.idByUrl(url);
        if (existing != null) {
            feed.refreshItem(url, title, body, charN);
            log.debug("同址重抓：{}", url);
            return new Ingested(existing, url, domain, -1, "同址重抓（不重判）", null, 0L, false, true);
        }

        // ② 闸 0（判的是可见性，不是存亡）
        Instant now = Instant.now();
        FeedGate.Verdict v = FeedGate.check(body, domain, publishedAt, now, gateConfig);

        // ③ simhash + 近重复
        long hash = Simhash.of(body);
        Long dupOf = null;
        Integer nearest = null;
        if (v.status() == FeedGate.Status.RECALLABLE) {
            // 一次算完：既是"是不是重复"的判定，也是"最近多近"的**读数**
            // （后者用来校准阈值 —— 判定与读数共用同一遍计算，不会各算各的）
            Near n = nearest(hash);
            dupOf = n.dupOf();
            nearest = n.dist();
        }

        // ④ 落库
        FeedItem item = new FeedItem();
        item.setSourceId(sourceId);
        item.setUrl(url);
        item.setTitle(title);
        item.setBody(body);
        item.setPublishedAt(publishedAt == null ? null : OffsetDateTime.ofInstant(publishedAt, ZoneId.systemDefault()));
        item.setSimhash(hash);
        item.setDupOf(dupOf);
        item.setStatus(v.status().code());
        item.setGateReason(dupOf == null ? v.reason() : v.reason() + "；近重复于 " + dupOf);
        item.setNearestDist(nearest);
        item.setCharN(charN);
        feed.insertItem(item);

        if (v.status() == FeedGate.Status.RECALLABLE) {
            long[] bands = Simhash.bands(hash);
            for (int i = 0; i < bands.length; i++) {
                feed.insertBand(item.getId(), i, bands[i]);
            }
        }
        log.info("信息流入库：{}｜{}｜{} 字｜{}｜{}", domain, shorten(title), charN,
                v.status() == FeedGate.Status.RECALLABLE ? "可召回" : "冷存",
                dupOf == null ? v.reason() : "近重复于 " + dupOf);
        return new Ingested(item.getId(), url, domain, v.status().code(), item.getGateReason(),
                dupOf, hash, dupOf != null, false);
    }

    /** 最近候选与它的距离。{@code dupOf} 为空表示没有近到算重复的。 */
    private record Near(Long dupOf, Integer dist) {
    }

    /**
     * 分带取候选 → 逐条算汉明距离 → **距离最小的那条**（并在 ≤ 阈值时指为重复）。
     *
     * <p>两个要点：
     * <ul>
     *   <li>分带只是**候选**条件，不是结论 —— 距离 &gt;3 的不算重复；</li>
     *   <li>命中的候选可能不止一条（同一个"重复堆"），**取 id 最小的那条**当规范条目，
     *       也就是"首见"。指向根而不是上一跳：否则"这个事件有几条报道"要递归才能数出来。</li>
     * </ul>
     */
    private Near nearest(long hash) {
        long[] b = Simhash.bands(hash);
        List<FeedMapper.Candidate> cands = feed.candidatesByBands(b[0], b[1], b[2], b[3]);
        int min = Integer.MAX_VALUE;
        Long firstSeen = null;
        for (FeedMapper.Candidate c : cands) {
            int d = Simhash.hamming(hash, c.simhash());
            min = Math.min(min, d);
            if (d <= Simhash.DUP_DISTANCE && (firstSeen == null || c.id() < firstSeen)) {
                firstSeen = c.id();
            }
        }
        return new Near(firstSeen, cands.isEmpty() ? null : min);
    }

    /** 读数。**近重复率是这一支的第一个关键数**：它决定"事件归并层"要不要做、做多细。 */
    public record Stats(long total, long unique, long dup, double dupRate,
                        long recallable, long cold, long sources) {
    }

    public Stats stats() {
        long uniq = feed.countUnique();
        long dup = feed.countDup();
        long total = uniq + dup;
        return new Stats(total, uniq, dup, total == 0 ? 0 : (double) dup / total,
                feed.countByStatus(FeedGate.Status.RECALLABLE.code()),
                feed.countByStatus(FeedGate.Status.COLD.code()),
                feed.countSources());
    }

    private Long ensureSource(String domain) {
        feed.insertSourceIfAbsent(domain, null);
        Long id = feed.sourceIdOf(domain);
        if (id == null) {
            // 理论上不该发生（刚插过）。真发生了宁可报错也别拿 null 去写条目 ——
            // 那会把 source_id 的 NOT NULL 变成一条看不懂的约束错误。
            throw new IllegalStateException("来源建不上：" + domain);
        }
        feed.touchSource(id);
        return id;
    }

    /** 取域名（小写、去掉 www.）。取不到就退回整条 URL —— 宁可难看，也不能丢来源。 */
    static String domainOf(String url) {
        try {
            String host = URI.create(url).getHost();
            if (host == null || host.isBlank()) {
                return url.toLowerCase(Locale.ROOT);
            }
            host = host.toLowerCase(Locale.ROOT);
            return host.startsWith("www.") ? host.substring(4) : host;
        } catch (Exception e) {
            return url.toLowerCase(Locale.ROOT);
        }
    }

    private static String shorten(String s) {
        if (s == null) {
            return "(无标题)";
        }
        return s.length() <= 28 ? s : s.substring(0, 28) + "…";
    }
}
