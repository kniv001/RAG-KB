package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.Sentence;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.Collection;
import java.util.List;

@Mapper
public interface SentenceMapper extends BaseMapper<Sentence> {

    /**
     * **一次取回这一批块的句子**（按块、按句号排好）。
     *
     * <p>刻意不用"每块查一次"：一次问答注入 10~15 块，逐块查就是十几次往返 ——
     * 而这条路径在**回答之前**，往返的每一毫秒都直接加在 TTFT 上。
     *
     * <p>{@code seq} 必须进排序：地址 {@code ⟨2.4⟩} 的后半段就是它，
     * 顺序错了地址就指错句。
     */
    @Select("""
            <script>
            SELECT * FROM sentences
            WHERE chunk_id IN
            <foreach item="id" collection="ids" open="(" separator="," close=")">#{id}</foreach>
            ORDER BY chunk_id, seq
            </script>
            """)
    List<Sentence> listByChunks(@Param("ids") Collection<Long> ids);

    /**
     * **分层注入**：只在已召回的这些块里，按问题挑最相关的 {@code limit} 句。
     *
     * <p>为什么是"先块后句"而不是全库挑句：实测全库平铺挑句的**精度只有 14.2%**
     * （同预算下块臂是 21.9%），而加上"块先框住范围"之后回到 **21.9%**、且
     * 靶子内容保留 30.6%、每题只要 1416 字（整块 6339 字）。
     *
     * <p>{@code embed_model} 必须过滤 —— 不同向量模型的输出不在同一空间，
     * 混着查会返回垃圾而**从结果上完全看不出来**（ChunkMapper 的类注释里同一条教训）。
     *
     * <p>{@code embedding IS NOT NULL} 也是必须的：没灌向量的行在 {@code <=>} 下是 NULL，
     * 会排到最前或最后（取决于空值处理），**静默挤掉真正相关的句子**。
     */
    @Select("""
            <script>
            SELECT id, chunk_id, doc_id, seq, char_start, char_end, kind, text, sent_hash, stamp
            FROM sentences
            WHERE chunk_id IN
            <foreach item="id" collection="ids" open="(" separator="," close=")">#{id}</foreach>
              AND embed_model = #{model}
              AND embedding IS NOT NULL
            ORDER BY embedding &lt;=&gt; #{q}::vector
            LIMIT #{limit}
            </script>
            """)
    List<Sentence> topInChunks(@Param("ids") Collection<Long> ids,
                               @Param("q") String vectorLiteral,
                               @Param("model") String embedModel,
                               @Param("limit") int limit);
}
