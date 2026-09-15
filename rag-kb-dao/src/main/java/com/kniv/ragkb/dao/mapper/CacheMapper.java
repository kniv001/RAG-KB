package com.kniv.ragkb.dao.mapper;

import com.kniv.ragkb.domain.dto.CachedAnswer;
import com.kniv.ragkb.domain.dto.CachedVector;
import org.apache.ibatis.annotations.*;

import java.util.List;
import java.util.Map;

/**
 * 三层缓存的读写。
 *
 * <p>键一律是内容哈希，<b>且把影响结果的参数都放进键里</b>（模型名、上下文哈希、历史哈希）。
 * 这样「改了输入就自动不命中」，不需要任何手工失效逻辑 ——
 * 缓存最大的坑就是静默返回陈旧结果，而它从输出上完全看不出来。
 *
 * <p>因此这里的清空操作只是释放空间，<b>不影响正确性</b>。
 */
@Mapper
public interface CacheMapper {

    // ---------------- 向量缓存 ----------------

    @Select("""
            <script>
            SELECT key, vec, dim FROM cache_embeddings WHERE key IN
            <foreach collection="keys" item="k" open="(" separator="," close=")">#{k}</foreach>
            </script>
            """)
    List<CachedVector> findEmbeddings(@Param("keys") List<String> keys);

    @Update("""
            <script>
            UPDATE cache_embeddings SET hits = hits + 1, last_hit_at = now() WHERE key IN
            <foreach collection="keys" item="k" open="(" separator="," close=")">#{k}</foreach>
            </script>
            """)
    int touchEmbeddings(@Param("keys") List<String> keys);

    @Insert("""
            <script>
            INSERT INTO cache_embeddings (key, model, vec, dim) VALUES
            <foreach collection="rows" item="r" separator=",">
                (#{r.key}, #{r.model}, #{r.vec}, #{r.dim})
            </foreach>
            ON CONFLICT (key) DO NOTHING
            </script>
            """)
    int insertEmbeddings(@Param("rows") List<Map<String, Object>> rows);

    // ---------------- 解析缓存 ----------------

    @Select("SELECT text FROM cache_parses WHERE key = #{key}")
    String findParse(@Param("key") String key);

    @Update("UPDATE cache_parses SET hits = hits + 1, last_hit_at = now() WHERE key = #{key}")
    int touchParse(@Param("key") String key);

    @Insert("""
            INSERT INTO cache_parses (key, name, text, chars) VALUES (#{key}, #{name}, #{text}, #{chars})
            ON CONFLICT (key) DO NOTHING
            """)
    int insertParse(@Param("key") String key, @Param("name") String name,
                    @Param("text") String text, @Param("chars") int chars);

    // ---------------- 回答缓存 ----------------

    @Select("""
            SELECT key, answer, provider, model, sources::text AS sources
            FROM cache_answers WHERE key = #{key}
            """)
    CachedAnswer findAnswer(@Param("key") String key);

    @Update("UPDATE cache_answers SET hits = hits + 1, last_hit_at = now() WHERE key = #{key}")
    int touchAnswer(@Param("key") String key);

    @Insert("""
            INSERT INTO cache_answers (key, question, answer, provider, model, sources)
            VALUES (#{key}, #{question}, #{answer}, #{provider}, #{model}, #{sources}::jsonb)
            ON CONFLICT (key) DO NOTHING
            """)
    int insertAnswer(@Param("key") String key, @Param("question") String question,
                     @Param("answer") String answer, @Param("provider") String provider,
                     @Param("model") String model, @Param("sources") String sources);

    // ---------------- 运维 ----------------

    @Select("""
            SELECT 'embeddings' AS kind, count(*) AS n, coalesce(sum(hits),0) AS hits, max(last_hit_at) AS last FROM cache_embeddings
            UNION ALL
            SELECT 'answers', count(*), coalesce(sum(hits),0), max(last_hit_at) FROM cache_answers
            UNION ALL
            SELECT 'parses', count(*), coalesce(sum(hits),0), max(last_hit_at) FROM cache_parses
            """)
    List<Map<String, Object>> stats();

    @Delete("DELETE FROM cache_embeddings")
    int clearEmbeddings();

    @Delete("DELETE FROM cache_answers")
    int clearAnswers();

    @Delete("DELETE FROM cache_parses")
    int clearParses();
}
