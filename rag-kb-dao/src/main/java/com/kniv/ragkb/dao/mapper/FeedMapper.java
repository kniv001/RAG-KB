package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.FeedItem;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

/**
 * 信息流（{@code news} 分支）的读写。
 *
 * <p>来源与条目两张表的操作放同一个接口：它们只服务同一件事（条目入库），
 * 拆成两个文件后"改一处忘一处"的概率比复用带来的收益大。
 */
@Mapper
public interface FeedMapper extends BaseMapper<FeedItem> {

    // ── 来源 ────────────────────────────────────────────────────────────────

    /** 来源不存在就建（权重给默认 0.5，**不猜**）；存在则什么都不改。 */
    @Insert("INSERT INTO feed_sources (domain, label) VALUES (#{domain}, #{label}) "
            + "ON CONFLICT (domain) DO NOTHING")
    int insertSourceIfAbsent(@Param("domain") String domain, @Param("label") String label);

    @Select("SELECT id FROM feed_sources WHERE domain = #{domain}")
    Long sourceIdOf(@Param("domain") String domain);

    /** 记一次抓取。⚠️ 统计量可以更新，条目本身不行。 */
    @Update("UPDATE feed_sources SET total_n = total_n + 1, last_fetch = now() WHERE id = #{id}")
    int touchSource(@Param("id") Long id);

    // ── 条目 ────────────────────────────────────────────────────────────────

    @Insert("INSERT INTO feed_items (source_id, url, title, body, published_at, simhash, "
            + "dup_of, status, gate_reason, nearest_dist, char_n) "
            + "VALUES (#{sourceId}, #{url}, #{title}, #{body}, #{publishedAt}, #{simhash}, "
            + "#{dupOf}, #{status}, #{gateReason}, #{nearestDist}, #{charN})")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insertItem(FeedItem item);

    /**
     * **同址重抓**：只更新抓取时间与正文，不新增行（{@code url} 上有唯一约束）。
     *
     * <p>不在这里改 {@code status} 与 {@code gate_reason}：可见性判定是**当时**做的，
     * 重抓不回头改写历史判断 —— 要改也得留下新记录（append-only 的精神）。
     */
    @Update("UPDATE feed_items SET fetched_at = now(), body = #{body}, title = #{title}, "
            + "char_n = #{charN} WHERE url = #{url}")
    int refreshItem(@Param("url") String url, @Param("title") String title,
                    @Param("body") String body, @Param("charN") int charN);

    @Select("SELECT id FROM feed_items WHERE url = #{url}")
    Long idByUrl(@Param("url") String url);

    // ── simhash 分带（LSH） ─────────────────────────────────────────────────

    @Insert("INSERT INTO feed_bands (item_id, band_no, band_key) VALUES (#{itemId}, #{bandNo}, #{bandKey})")
    int insertBand(@Param("itemId") Long itemId, @Param("bandNo") int bandNo,
                   @Param("bandKey") long bandKey);

    /** 一条候选（近重复的**候选**，不是结论 —— 还要算汉明距离）。 */
    record Candidate(long id, long simhash) {
    }

    /**
     * 按分带取候选：**任意一段相同**即可。
     *
     * <p>四段写成四个 OR 而不是 {@code (band_no, band_key) IN (...)} + {@code foreach}：
     * 段数固定为 {@code Simhash.BANDS}，而 OR 形式**不需要传集合**（集合参数在 MyBatis 里
     * 要么拼 string，要么依赖 record 的属性名解析 —— 后者在 record 上没有 getId() 之类的
     * getter 时不一定取得到值）。四个 OR 走 BitmapOr，一样吃得下
     * {@code feed_bands_lookup_idx}。
     *
     * <p>{@code ORDER BY i.id}：候选通常是同一个"重复堆"里的几条，
     * **要指回最早那条**，所以按 id 升序。
     */
    @Select("""
            SELECT DISTINCT i.id AS id, i.simhash AS simhash
            FROM feed_bands b JOIN feed_items i ON i.id = b.item_id
            WHERE (b.band_no = 0 AND b.band_key = #{k0})
               OR (b.band_no = 1 AND b.band_key = #{k1})
               OR (b.band_no = 2 AND b.band_key = #{k2})
               OR (b.band_no = 3 AND b.band_key = #{k3})
            ORDER BY i.id
            LIMIT 200
            """)
    List<Candidate> candidatesByBands(@Param("k0") long k0, @Param("k1") long k1,
                                      @Param("k2") long k2, @Param("k3") long k3);

    // ── 抓取调度（频道层） ──────────────────────────────────────────────────

    /**
     * 待轮询的频道：**按层级、再按最久没抓的排**。
     *
     * <p>排序就是"分层推进"的落点：tier 1（国家级）永远排在门户/垂媒前面 ——
     * 一轮的抓取预算有限时，先保证层级高的源被覆盖。
     * {@code NULLS FIRST} 让没抓过的排在抓过的前面（冷启动先铺满）。
     */
    // ⚠️ **别名必须加双引号** —— PostgreSQL 把未加引号的别名折成小写（`AS fTitle` → `ftitle`），
    // 而 MyBatis 的 map 键就是**结果集里的名字**，于是 Java 侧 `get("fTitle")` 永远拿到 null。
    // 症状极隐蔽：映射变成空串 ⇒ 条目全被丢掉 ⇒ 报"feed 空"（看起来像源头没新闻）。
    // **同一个坑本项目已踩两次**（前一次是 FeedIndexMapper 的 `itemN`）——
    // 凡是从 map 里按驼峰名取值，SQL 别名就一律加引号。
    @Select("""
            SELECT c.id, c.url, c.label, c.kind, c.array_path AS "arrayPath",
                   c.f_title AS "fTitle", c.f_link AS "fLink", c.f_date AS "fDate",
                   c.source_id AS "sourceId", c.last_item_at AS "lastItemAt",
                   s.domain, s.tier
            FROM feed_channels c JOIN feed_sources s ON s.id = c.source_id 
            WHERE c.enabled AND s.tier <= #{tierMax}
            ORDER BY s.tier ASC, c.last_fetch ASC NULLS FIRST
            """)
    List<java.util.Map<String, Object>> pollableChannels(@Param("tierMax") int tierMax);

    @Update("UPDATE feed_channels SET last_fetch = now(), last_item_at = "
            + "GREATEST(COALESCE(last_item_at, 'epoch'::timestamptz), COALESCE(#{lastItemAt}, 'epoch'::timestamptz)), "
            + "item_n = item_n + #{added}, last_error = NULL WHERE id = #{id}")
    int markChannelFetched(@Param("id") long id, @Param("lastItemAt") java.time.OffsetDateTime lastItemAt,
                           @Param("added") int added);

    @Update("UPDATE feed_channels SET last_fetch = now(), err_n = err_n + 1, last_error = #{error} "
            + "WHERE id = #{id}")
    int markChannelError(@Param("id") long id, @Param("error") String error);

    // ── 晋升（信息流 → 知识库文档）──────────────────────────────────────────

    /**
     * 够格的晋升候选。
     *
     * <p>三条规则都写在 SQL 里而不是 Java 里：它们描述的是**数据状态**（可召回、非转载、
     * 议题够大），放在一处能一眼看全，也便于将来用一条 SQL 回答"为什么这条没进库"。
     *
     * <p>⚠️ **别名加引号**（`AS "publishedAt"`）：PostgreSQL 会把未加引号的折成小写，
     * 而 MyBatis 的 map 键就是结果集里的名字 —— 这个坑本项目已踩两次。
     */
    @Select("""
            SELECT i.id, i.title, i.body, i.url, i.published_at AS "publishedAt",
                   t.item_n AS "topicN"
            FROM feed_items i
            JOIN feed_item_topics e ON e.item_id = i.id
            JOIN feed_topics t ON t.id = e.topic_id
            WHERE i.status = 1 AND i.dup_of IS NULL AND i.doc_id IS NULL
              AND t.item_n >= #{minTopicItems}
            ORDER BY t.item_n DESC, i.published_at DESC NULLS LAST
            LIMIT #{limit}
            """)
    List<java.util.Map<String, Object>> promotable(@Param("limit") int limit,
                                                   @Param("minTopicItems") int minTopicItems);

    @Update("UPDATE feed_items SET doc_id = #{docId} WHERE id = #{id}")
    int markPromoted(@Param("id") long id, @Param("docId") String docId);

    @Select("SELECT count(*) FROM feed_items WHERE doc_id IS NOT NULL")
    long countPromoted();

    /**
     * **全部可召回条目的正文** —— 用来算"哪些行是站点家具"。
     *
     * <p>为什么按行频而不是写规则：实测（2026-09-24）—— 按"连续短行"猜，误伤率 5%、
     * 与真删的 14% 一个量级（**精度约等于抛硬币**）；而按"这行出现在多少个不同条目里"，
     * 前几名是「发表评论」410 次、「大字体」408 次、「来源：中国新闻网」322 次 ——
     * **每一行都是页面家具，没有一条是内容**。
     *
     * <p>只取 {@code status = 1}：冷存的垃圾不该参与"什么算家具"的判断。
     */
    @Select("SELECT body FROM feed_items WHERE status = 1 AND body IS NOT NULL")
    List<String> allBodies();

    /**
     * **某条目里被判为模板的段落区间**（在 body 里的字符位置）。
     *
     * <p>为什么用这个而不是再写一条文本规则：实测（2026-09-24）—— 模板登记表
     * （{@code feed_boilerplate}，按**相似度**认家具）已经标出这些正文的 **12~64%**
     * （多数 30~58%），而那正是导航/侧栏那些块。**它早就算好了，只是晋升那一步没用它。**
     *
     * <p>而文本规则试过两条都不行：「连续短行」（卡 ≤12 字）漏掉了 15~24 字的那批；
     * 「跨条目行频」（放开长度上限）在长侧栏上只命中 2 行（侧栏里的日期每页都在变，
     * 逐字比对自然过不了）。
     */
    @Select("""
            SELECT s.char_start AS s, s.char_end AS e
            FROM feed_segments s JOIN feed_boilerplate b ON b.hash = md5(s.text)
            WHERE s.item_id = #{itemId}
            ORDER BY s.char_start
            """)
    List<java.util.Map<String, Object>> boilerplateRanges(@Param("itemId") long itemId);

    // ── 读数（量重复率用；也是这个方向的第一批"尺子"） ──────────────────────

    /** 首见条目数。 */
    @Select("SELECT count(*) FROM feed_items WHERE dup_of IS NULL")
    long countUnique();

    /** 近重复条目数。 */
    @Select("SELECT count(*) FROM feed_items WHERE dup_of IS NOT NULL")
    long countDup();

    @Select("SELECT count(*) FROM feed_items WHERE status = #{status}")
    long countByStatus(@Param("status") int status);

    @Select("SELECT count(DISTINCT source_id) FROM feed_items")
    long countSources();

    /**
     * **近重复距离的分布** —— 校准 {@code Simhash.DUP_DISTANCE} 的唯一依据。
     *
     * <p>按"有候选的条目"分组统计：{@code no_candidate} 那一档说明**连一段都没撞上**，
     * 那是 LSH 的盲区（真正的最近距离无从知道），要单独看，不能混进"距离很大"里 ——
     * 两者含义完全不同：一个是"确实不重复"，一个是"没比过"。
     */
    @Select("SELECT nearest_dist AS d, count(*) AS n FROM feed_items "
            + "GROUP BY nearest_dist ORDER BY nearest_dist NULLS LAST")
    List<java.util.Map<String, Object>> distHistogram();
}
