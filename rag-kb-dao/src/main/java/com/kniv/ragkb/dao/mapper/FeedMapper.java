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
