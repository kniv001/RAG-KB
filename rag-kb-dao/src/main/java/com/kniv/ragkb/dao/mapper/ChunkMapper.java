package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.dto.ChunkHit;
import com.kniv.ragkb.domain.entity.Chunk;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

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
            SELECT c.id, c.doc_id, c.seq, c.content, d.name AS doc_name,
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
            SELECT c.id, c.doc_id, c.seq, c.content, d.name AS doc_name,
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

    @Delete("DELETE FROM chunks WHERE doc_id = #{docId}")
    int deleteByDoc(@Param("docId") String docId);

    @Select("SELECT count(*) FROM chunks WHERE doc_id = #{docId}")
    int countByDoc(@Param("docId") String docId);

    /** 缓存命中率检查用：看某个模型的向量是否已就位 */
    @Select("SELECT count(*) FROM chunks WHERE embed_model = #{model} AND embedding IS NOT NULL")
    int countIndexedWithModel(@Param("model") String model);
}
