package com.kniv.ragkb.dao.mapper;

import com.kniv.ragkb.domain.entity.FeedTopic;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;
import java.util.Map;

/**
 * 信息流的**第 2、3 层**：段落向量（重复判定）与议题（事件归并）。
 *
 * <p>与 {@link FeedMapper} 分开：那个管"条目本身"（append-only 的事实），
 * 这个管**派生结构**（段落索引、议题质心）—— 两类东西的写入规则不同
 * （前者只 INSERT，后者允许更新统计量），混在一个接口里迟早会写错。
 */
@Mapper
public interface FeedIndexMapper {

    // ── 条目向量与读数 ──────────────────────────────────────────────────────

    /** 写条目的 lead 向量 + 段落统计 + 最近议题相似度，并标记已富化。 */
    @Update("UPDATE feed_items SET embedding = #{vec}::vector, embed_model = #{model}, "
            + "seg_n = #{segN}, dup_seg_n = #{dupN}, nearest_topic_sim = #{topicSim}, "
            + "enriched_at = now() WHERE id = #{id}")
    int updateEnriched(@Param("id") long id, @Param("vec") String vecLiteral,
                       @Param("model") String model, @Param("segN") int segN,
                       @Param("dupN") int dupN, @Param("topicSim") Float topicSim);

    /** 待富化的条目：可召回、且还没算过。按 id 升序（先来先算）。 */
    @Select("SELECT id, title, body FROM feed_items "
            + "WHERE status = 1 AND enriched_at IS NULL ORDER BY id LIMIT #{limit}")
    List<Map<String, Object>> pendingEnrich(@Param("limit") int limit);

    @Select("SELECT id, title, body FROM feed_items WHERE id = #{id}")
    Map<String, Object> itemById(@Param("id") long id);

    // ── 段落 ────────────────────────────────────────────────────────────────

    @Insert("INSERT INTO feed_segments (item_id, seq, text, char_start, char_end, embedding, "
            + "embed_model, best_sim) "
            + "VALUES (#{itemId}, #{seq}, #{text}, #{start}, #{end}, #{vec}::vector, #{model}, #{bestSim})")
    int insertSegment(@Param("itemId") long itemId, @Param("seq") int seq, @Param("text") String text,
                      @Param("start") int start, @Param("end") int end,
                      @Param("vec") String vecLiteral, @Param("model") String model,
                      @Param("bestSim") Float bestSim);

    /**
     * **段落级回归测试**：这一片内容库里有没有？
     *
     * <p>排除同一篇（{@code item_id <> #{itemId}}）—— 否则一篇稿子里的重复句会自己命中自己。
     * 返回最近邻的相似度（余弦，1 - 距离），由调用方与阈值比较。
     *
     * <p>⚠️ 只比**可召回**的条目：垃圾页高度自相似，把它们放进比对池，
     * 会让正常文章被判成"跟某导航页重复"（与 LSH 索引同一条理由）。
     */
    @Select("SELECT 1 - (s.embedding <=> #{vec}::vector) AS sim "
            + "FROM feed_segments s JOIN feed_items i ON i.id = s.item_id "
            + "WHERE s.embed_model = #{model} AND s.item_id <> #{itemId} AND i.status = 1 "
            + "AND NOT EXISTS (SELECT 1 FROM feed_boilerplate b WHERE b.hash = md5(s.text)) "
            + "ORDER BY s.embedding <=> #{vec}::vector LIMIT 1")
    List<Double> nearestSegmentSim(@Param("vec") String vecLiteral, @Param("model") String model,
                                   @Param("itemId") long itemId);

    @Select("SELECT count(*) FROM feed_segments WHERE item_id = #{itemId}")
    long countSegments(@Param("itemId") long itemId);

    /**
     * **登记模板段**：同一段文字在 ≥{@code minItems} 个不同条目里出现 ⇒ 它是站点家具。
     *
     * <p>写进 `feed_boilerplate`（按文本哈希）而**不是**标在段落行上 ——
     * 段落会随切分器/粒度/重跑而重建，标在段上等于每次重建都要重学一遍，
     * 而重学期间的读数又是脏的。
     */
    @Insert("INSERT INTO feed_boilerplate (hash, n_items, sample) "
            + "SELECT md5(text), count(DISTINCT item_id), min(left(replace(text, E'\n', ' '), 120)) "
            + "FROM feed_segments GROUP BY md5(text) "
            + "HAVING count(DISTINCT item_id) >= #{minItems} "
            + "ON CONFLICT (hash) DO UPDATE SET n_items = EXCLUDED.n_items")
    int markBoilerplate(@Param("minItems") int minItems);

    /** 模板占比（读数）—— 它有多大，直接决定 dup_ratio 有多脏。 */
    @Select("SELECT (SELECT count(*) FROM feed_segments s WHERE EXISTS ("
            + "  SELECT 1 FROM feed_boilerplate b WHERE b.hash = md5(s.text))), "
            + "(SELECT count(*) FROM feed_segments)")
    List<Map<String, Object>> boilerplateStats();

    // ── 议题 ────────────────────────────────────────────────────────────────

    /**
     * 找**近 N 天**内最像的议题。
     *
     * <p>时间窗是**必须的**：新闻的事件是有寿命的，不设窗的话"金价"会把半年前的另一件事
     * 并进来，议题就退化成主题（而主题是另一层的东西）。
     */
    // **别名要加引号**：PostgreSQL 会把未加引号的别名折成小写（itemN → itemn），
    // 而 MyBatis 的 map 键用的就是结果集里的名字 —— 不加引号取到的永远是 null。
    @Select("SELECT id, embedding::text AS emb, item_n AS \"itemN\", "
            + "1 - (embedding <=> #{vec}::vector) AS sim "
            + "FROM feed_topics WHERE embed_model = #{model} "
            + "AND last_seen > now() - (#{days} || ' days')::interval "
            + "ORDER BY embedding <=> #{vec}::vector LIMIT 1")
    List<Map<String, Object>> nearestTopic(@Param("vec") String vecLiteral,
                                           @Param("model") String model,
                                           @Param("days") int windowDays);

    // ⚠️ 多参数时参数对象是个 map，取属性必须带前缀：`#{label}` 会报
    // "Parameter 'label' not found. Available parameters are [vec, topic, model, ...]"。
    @Insert("INSERT INTO feed_topics (label, embedding, embed_model, item_n) "
            + "VALUES (#{topic.label}, #{vec}::vector, #{model}, 1)")
    @org.apache.ibatis.annotations.Options(useGeneratedKeys = true, keyProperty = "topic.id")
    int insertTopic(@Param("topic") FeedTopic topic, @Param("vec") String vecLiteral,
                    @Param("model") String model);

    /**
     * 质心更新：**新均值 = (旧均值 × n + 新向量) / (n+1)**，在 Java 侧算好再写回。
     *
     * <p>为什么不在 SQL 里算：**pgvector 0.8 没有标量乘**（实测 `vector * float8` 直接报
     * operator does not exist）。硬要写成 `array_fill(...)::vector` 的元素乘也不是不行，
     * 但可读性崩掉、而这条路径是后台批处理、不在乎一次往返。
     *
     * <p>⚠️ 读-改-写有竞态：本路径**单线程跑**（见 {@code FeedEnrichService}），
     * 要并发化必须先把这一步换掉。
     */
    @Update("UPDATE feed_topics SET embedding = #{vec}::vector, item_n = #{itemN}, "
            + "last_seen = now() WHERE id = #{id}")
    int updateTopicCentroid(@Param("id") long id, @Param("vec") String vecLiteral,
                            @Param("itemN") int itemN);

    @Insert("INSERT INTO feed_item_topics (item_id, topic_id, sim) VALUES (#{itemId}, #{topicId}, #{sim}) "
            + "ON CONFLICT (item_id, topic_id) DO NOTHING")
    int linkItemTopic(@Param("itemId") long itemId, @Param("topicId") long topicId,
                      @Param("sim") float sim);

    @Select("SELECT COALESCE(label, '(未命名)') AS label, item_n AS n, "
            + "first_seen AS f, last_seen AS l, id "
            + "FROM feed_topics ORDER BY item_n DESC, last_seen DESC LIMIT #{limit}")
    List<Map<String, Object>> topTopics(@Param("limit") int limit);

    @Select("SELECT embedding::text FROM feed_topics WHERE id = #{id}")
    String topicEmbeddingText(@Param("id") long id);

    // ── 议题命名（label）────────────────────────────────────────────────────

    /**
     * 还没起名的议题 + 它们的成员标题（最多 8 条，最近的在前）。
     *
     * <p>用 {@code chr(10)} 拼行而不是 SQL 的换行转义：本项目的工具链今天已经被
     * "转义序列被吃掉"咬了五次（`\r?\n` 变成真的 CR/LF 之类）—— **能不写转义就不写**。
     */
    @Select("""
            SELECT t.id,
                   string_agg(x.title, chr(10) ORDER BY x.published_at DESC NULLS LAST) AS titles,
                   count(*) AS n
            FROM feed_topics t
            JOIN (
              SELECT e.topic_id, left(i.title, 60) AS title, i.published_at
              FROM feed_item_topics e JOIN feed_items i ON i.id = e.item_id
              WHERE i.title IS NOT NULL AND i.title <> ''
            ) x ON x.topic_id = t.id
            WHERE t.label IS NULL
            GROUP BY t.id
            ORDER BY count(*) DESC, t.id
            LIMIT #{limit}
            """)
    List<Map<String, Object>> unlabeledTopics(@Param("limit") int limit);

    @Update("UPDATE feed_topics SET label = #{label} WHERE id = #{id}")
    int setTopicLabel(@Param("id") long id, @Param("label") String label);
}
