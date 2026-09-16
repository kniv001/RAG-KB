package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.Message;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
public interface MessageMapper extends BaseMapper<Message> {

    @Select("""
            SELECT id, conv_id, role, content, sources, provider, model, created_at
            FROM messages WHERE conv_id = #{convId} ORDER BY id
            """)
    List<Message> listByConversation(@Param("convId") String convId);

    /**
     * 取最近 N 条历史给模型看。
     *
     * <p>倒序取再在上层翻转，而不是正序取全部再截断 —— 后者在长会话下会把
     * 整张表读进来。只选 role 与 content：历史里不需要 sources，
     * 带上会白占上下文预算。
     */
    @Select("""
            SELECT id, conv_id, role, content, provider, model, created_at
            FROM messages WHERE conv_id = #{convId}
            ORDER BY id DESC LIMIT #{limit}
            """)
    List<Message> recentForContext(@Param("convId") String convId, @Param("limit") int limit);

    @Select("SELECT count(*) FROM messages WHERE conv_id = #{convId}")
    int countByConversation(@Param("convId") String convId);

    // ---------------- 历史索引 ----------------

    /**
     * 取还没向量化的消息，用于增量索引。
     *
     * <p>只索引增量而不是每次重算整个会话：向量化是这条链上最贵的一步，
     * 而历史只会追加、不会改写。
     */
    @Select("""
            SELECT id, conv_id, role, content FROM messages
            WHERE conv_id = #{convId} AND embedding IS NULL AND content <> ''
            ORDER BY id LIMIT #{limit}
            """)
    List<Message> pendingIndex(@Param("convId") String convId, @Param("limit") int limit);

    @Update("UPDATE messages SET embedding = #{vec}::vector, embed_model = #{model} WHERE id = #{id}")
    int setEmbedding(@Param("id") long id, @Param("vec") String vectorLiteral,
                     @Param("model") String embedModel);

    /**
     * 在本会话内做向量召回。
     *
     * <p>与 {@code chunks} 的检索刻意分开：那是全局知识库，这是单个会话的历史，
     * 混在一起会让「上次我们聊到哪」被无关文档淹没。
     *
     * <p>{@code embed_model} 过滤不能省：不同向量模型的输出不在同一空间，
     * 混着查会静默返回垃圾结果，而且从结果上完全看不出来。
     *
     * <p><b>排除为什么下推到 SQL 而不是取回来再过滤</b>：先取「最近的 N 条」再过滤的话，
     * 那 N 条很可能**全在排除集里**（比如最近窗口的那几条），过滤完就成了空 ——
     * 表现是「明明库里有、却一条也召不回」。下推之后数据库是在**未被排除的**里面
     * 取最近的 N 条，这才是想要的语义。实测踩过：裁剪历史时召回的片段一直是 0 字。
     *
     * <p>空数组时 {@code = ANY('{}')} 恒为假，取反恒为真，等于不排除 —— 正好。
     *
     * <p><b>{@code <=>} 必须写成字面量，不能写成 {@code &lt;=&gt;}。</b>
     * XML 实体只在 {@code <script>} 块里才被解码 —— ChunkMapper 的同类查询有
     * {@code <script>}（因为它要用 {@code <if>}），所以那边写转义是对的；
     * 照抄到没有 script 的注解上，转义符会原样发给数据库，直接语法错。
     */
    @Select("""
            SELECT id, conv_id, role, content, index_text, index_topic,
                   (embedding <=> #{q}::vector) AS distance
            FROM messages
            WHERE conv_id = #{convId}
              AND embedding IS NOT NULL
              AND embed_model = #{model}
              AND NOT (id = ANY(#{exclude}::bigint[]))
            ORDER BY embedding <=> #{q}::vector
            LIMIT #{limit}
            """)
    List<Message> searchByVector(@Param("convId") String convId,
                                 @Param("q") String vectorLiteral,
                                 @Param("model") String embedModel,
                                 @Param("exclude") Long[] excludeIds,
                                 @Param("limit") int limit);

    /**
     * 把命中的消息连同它前后各一条一起取出来。
     *
     * <p>一轮对话由相邻的两条（用户提问 + 助手回答）组成。只返回命中的那一条，
     * 模型会看到一段没有来由的回答；带上邻居才是一段能读懂的历史片段。
     * 多带一条的代价可以忽略，而少了它整段就没用。
     */
    @Select("""
            <script>
            SELECT id, conv_id, role, content, index_text, index_topic FROM messages
            WHERE conv_id = #{convId} AND id IN (
                SELECT unnest(#{ids}::bigint[])
                UNION SELECT unnest(#{ids}::bigint[]) - 1
                UNION SELECT unnest(#{ids}::bigint[]) + 1
            )
            ORDER BY id
            </script>
            """)
    List<Message> withNeighbors(@Param("convId") String convId, @Param("ids") Long[] ids);

    @Select("SELECT count(*) FROM messages WHERE conv_id = #{convId} AND embedding IS NOT NULL")
    int countIndexed(@Param("convId") String convId);

    // ---------------- 轮次笔记 ----------------

    /**
     * 取需要改写的轮次：窗口之外、还没有笔记的助手消息。
     *
     * <p>只挑 role='assistant'：笔记的内容主体是回答，提问由
     * {@link #questionBefore} 单独取。窗口内的不动 —— 那些原文本来就在
     * 提示词里，改写它们是重复劳动。
     *
     * <p>{@code index_text IS NULL} 同时起到「未处理」标记的作用：
     * 改写失败或判定不可用时写空串（见 {@link #markNoteSkipped}），
     * 于是不会每轮都重试同一条。
     */
    @Select("""
            SELECT id, conv_id, role, content FROM messages
            WHERE conv_id = #{convId} AND role = 'assistant'
              AND index_text IS NULL AND content <> ''
              AND id <= #{uptoId}
            ORDER BY id LIMIT #{limit}
            """)
    List<Message> turnsWithoutNote(@Param("convId") String convId,
                                   @Param("uptoId") long uptoId,
                                   @Param("limit") int limit);

    /** 这一轮的提问：该助手消息之前最近的一条用户消息 */
    @Select("""
            SELECT id, conv_id, role, content FROM messages
            WHERE conv_id = #{convId} AND role = 'user' AND id < #{beforeId}
            ORDER BY id DESC LIMIT 1
            """)
    Message questionBefore(@Param("convId") String convId, @Param("beforeId") long beforeId);

    /** 写入笔记，并**用笔记重算向量** —— 检索键从此是笔记，不再是原文 */
    @Update("""
            UPDATE messages SET index_text = #{text}, index_topic = #{topic},
                   embedding = #{vec}::vector, embed_model = #{model}
            WHERE id = #{id}
            """)
    int setNote(@Param("id") long id, @Param("text") String text, @Param("topic") String topic,
                @Param("vec") String vectorLiteral, @Param("model") String embedModel);

    /**
     * 标记「这条不用改写」，避免每轮重试。
     *
     * <p>写空串而不是保留 NULL：查询条件是 {@code index_text IS NULL}，
     * 空串不等于 NULL，于是它会被跳过。
     */
    @Update("UPDATE messages SET index_text = '' WHERE id = #{id}")
    int markNoteSkipped(@Param("id") long id);

    // ---------------- 滚动摘要 ----------------

    @Select("SELECT COALESCE(max(id), 0) FROM messages WHERE conv_id = #{convId}")
    long maxId(@Param("convId") String convId);

    /**
     * 取倒数第 {@code offset} 条消息的 id —— 也就是「最近窗口之外最新的那一条」。
     *
     * <p>不能用 {@code maxId - offset} 代替：消息 id 是全局序列，别的会话会插进来，
     * 单个会话里根本不连续。只有按 conv_id 排序取偏移量才是准的。
     *
     * @return 消息数不足 offset 时返回 null（说明还没有东西掉出窗口）
     */
    @Select("SELECT id FROM messages WHERE conv_id = #{convId} ORDER BY id DESC OFFSET #{offset} LIMIT 1")
    Long idAtOffset(@Param("convId") String convId, @Param("offset") int offset);

    /**
     * 取一段 id 区间内的消息，供增量摘要把「新掉出窗口」的那几条并进去。
     *
     * <p>两端的比较都是 {@code >} 与 {@code <=}：{@code afterId} 是摘要已经覆盖到的位置，
     * 用开区间才不会把同一条消息重复并进去。
     */
    @Select("""
            SELECT id, conv_id, role, content FROM messages
            WHERE conv_id = #{convId} AND id > #{afterId} AND id <= #{uptoId}
            ORDER BY id LIMIT #{limit}
            """)
    List<Message> listBetween(@Param("convId") String convId,
                              @Param("afterId") long afterId,
                              @Param("uptoId") long uptoId,
                              @Param("limit") int limit);
}
