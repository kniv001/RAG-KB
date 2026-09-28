package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.domain.entity.Chunk;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.Collection;
import java.util.List;

/**
 * 分块与向量检索。
 *
 * <p>两条通道对应混合检索的两路召回：
 * <ul>
 *   <li>{@link #searchByVector} —— pgvector 余弦距离，走 HNSW 索引</li>
 *   <li>{@link #searchByKeyword} —— 中文二元组 + ILIKE 计数打分</li>
 * </ul>
 * 两者分数尺度完全不同（距离 vs 命中数），所以融合必须用 RRF 这类只看排名的算法。
 *
 * <p>检索一律带 {@code embed_model} 过滤：不同向量模型的输出不在同一空间，
 * 混着查会静默返回垃圾结果，而这种错误从结果上完全看不出来。
 */
@Mapper
public interface ChunkMapper extends BaseMapper<Chunk> {

    /** 向量召回。{@code #{q}::vector} 的显式转型是必须的 —— 参数是字符串。 */
    @Select("""
            <script>
            SELECT c.id, c.doc_id, c.seq, c.content, c.ctx, d.name AS doc_name,
                   d.source_kind, d.source_url, d.fetched_at,
                   (c.embedding &lt;=&gt; #{q}::vector) AS distance
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE c.embedding IS NOT NULL
              AND c.embed_model = #{model}
            <if test="docId != null"> AND c.doc_id = #{docId} </if>
            ORDER BY c.embedding &lt;=&gt; #{q}::vector
            LIMIT #{limit}
            </script>
            """)
    List<ChunkHit> searchByVector(@Param("q") String vectorLiteral,
                                  @Param("model") String embedModel,
                                  @Param("docId") String docId,
                                  @Param("limit") int limit);

    /**
     * 关键词召回：对每个检索词统计出现次数作为分数。
     *
     * <p>用 ILIKE 而非 PostgreSQL 全文检索：后者的 simple 分词器按非字母数字切词，
     * 中文整句没有空格，会被当成一个 token，等于不可用。二元组 + ILIKE 是个人规模下
     * 最简单且真的有效的中文关键词方案。
     */
    @Select("""
            <script>
            SELECT c.id, c.doc_id, c.seq, c.content, c.ctx, d.name AS doc_name,
                   d.source_kind, d.source_url, d.fetched_at,
                   (SELECT count(*) FROM unnest(ARRAY[
                       <foreach collection="terms" item="t" separator=",">#{t}</foreach>
                   ]::text[]) AS x WHERE c.content ILIKE '%' || x || '%') AS hits
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE c.embed_model = #{model}
            <if test="docId != null"> AND c.doc_id = #{docId} </if>
            ORDER BY hits DESC, c.id
            LIMIT #{limit}
            </script>
            """)
    List<ChunkHit> searchByKeyword(@Param("terms") List<String> terms,
                                   @Param("model") String embedModel,
                                   @Param("docId") String docId,
                                   @Param("limit") int limit);

    /**
     * **相邻块补全**：给每个命中块，把它同文档里 {@code seq±span} 的块取回来。
     *
     * <p>它不是"扩召回"，是**补全** —— 命中的那块是对的，但它常常只是**半句话**：
     * 分块按长度切，答案落在哪一块是运气。2026-09-26 量过四种"边"（判据 = **靶句**进没进，
     * multihop-127）：相邻块 {@code seq±1} **120/127 @72 句 / 16 块**，语义 kNN 图 114@88
     * （命中更低、代价更高）、同文档 121@198、同簇 122@499（拖进 111 块），
     * 而**随机块对照 113 = 一点没涨** ⇒ 增益来自"这条边有信息"，不是"加得多"。
     *
     * <p>这条 SQL 之所以是自连接而不是"先查 doc_id 再查 seq"：一次往返就能补完所有命中，
     * 而它在**回答之前**，往返的每一毫秒都直接加在 TTFT 上（与 {@link #searchByVector} 同一条理由）。
     * {@code (doc_id, seq)} 上有唯一约束索引（{@code chunks_doc_id_seq_key}），
     * 所以这个自连接走索引，不扫表。
     *
     * <p>{@code distance} / {@code hits} 都留空：这两种分数是**召回通道**的产物，
     * 补全块没有参与召回 —— 给它们编一个分数只会让 {@code rank()} 的排序说谎。
     */
    @Select("""
            <script>
            SELECT n.id, n.doc_id, n.seq, n.content, n.ctx, d.name AS doc_name,
                   d.source_kind, d.source_url, d.fetched_at,
                   NULL::float8 AS distance, NULL::int AS hits
            FROM chunks c
            JOIN chunks n ON n.doc_id = c.doc_id
                         AND n.seq BETWEEN c.seq - #{span} AND c.seq + #{span}
                         AND n.seq &lt;&gt; c.seq
            JOIN documents d ON d.id = n.doc_id
            WHERE c.id IN
            <foreach item="id" collection="ids" open="(" separator="," close=")">#{id}</foreach>
              AND n.embed_model = #{model}
            ORDER BY n.doc_id, n.seq
            </script>
            """)
    List<ChunkHit> listNeighbors(@Param("ids") Collection<Long> ids,
                                 @Param("span") int span,
                                 @Param("model") String embedModel);

    @Delete("DELETE FROM chunks WHERE doc_id = #{docId}")
    int deleteByDoc(@Param("docId") String docId);

    @Select("SELECT count(*) FROM chunks WHERE doc_id = #{docId}")
    int countByDoc(@Param("docId") String docId);

    /** 缓存命中率检查用：看某个模型的向量是否已就位 */
    @Select("SELECT count(*) FROM chunks WHERE embed_model = #{model} AND embedding IS NOT NULL")
    int countIndexedWithModel(@Param("model") String model);
}
