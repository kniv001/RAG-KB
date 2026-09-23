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

    /**
     * **逐块挑句**：每个块各出它自己最相关的 {@code k} 句。
     *
     * <p>与 {@link #topInChunks} 的差别不在 SQL 技巧，而在**形状**：
     * 那个是**全局** {@code LIMIT M}，于是**没挤进全局前 M 名的块一句都拿不到** ——
     * 而调用方对"这块没句子"的退路是**注入整块**（那是给"表没建/戳过期"准备的兜底）。
     * 实测（2026-09-23，43 次调用）：平均 10.2 段里 8.2 段有句子，**平均 2.0 段退回整块**，
     * 29/43 次调用至少有一段退回 ⇒ 形状与设计意图相反：
     * <b>最相关的块被截成几句，最不相关的块反而装全文</b>。
     *
     * <p>逐块挑句之后，**每个有句子的块都至少贡献一句**，兜底只剩"真的切不出句子"那一种，
     * 形状就正过来了。{@code k} 取多少是**预算**问题：块数 × k 就是注入句数的上界。
     *
     * <p>{@code embed_model} / {@code embedding IS NOT NULL} 两条过滤的理由同
     * {@link #topInChunks}，一个都不能少。
     */
    @Select("""
            <script>
            SELECT id, chunk_id, doc_id, seq, char_start, char_end, kind, text, sent_hash, stamp
            FROM (
              SELECT id, chunk_id, doc_id, seq, char_start, char_end, kind, text, sent_hash, stamp,
                     row_number() OVER (PARTITION BY chunk_id
                                        ORDER BY embedding &lt;=&gt; #{q}::vector) AS rn
              FROM sentences
              WHERE chunk_id IN
              <foreach item="id" collection="ids" open="(" separator="," close=")">#{id}</foreach>
                AND embed_model = #{model}
                AND embedding IS NOT NULL
            ) t
            WHERE rn &lt;= #{k}
            ORDER BY chunk_id, rn
            </script>
            """)
    List<Sentence> topPerChunkInChunks(@Param("ids") Collection<Long> ids,
                                       @Param("q") String vectorLiteral,
                                       @Param("model") String embedModel,
                                       @Param("k") int k);
}
