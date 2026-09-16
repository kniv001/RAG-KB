package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.Conversation;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
public interface ConversationMapper extends BaseMapper<Conversation> {

    /** 会话列表，带消息条数。按最近活跃排序 —— 用得多的一定在最上面。 */
    @Select("""
            SELECT c.*, (SELECT count(*) FROM messages m WHERE m.conv_id = c.id) AS turns
            FROM conversations c
            ORDER BY c.updated_at DESC
            LIMIT #{limit}
            """)
    List<Conversation> listWithTurns(@Param("limit") int limit);

    @Update("""
            UPDATE conversations
            SET updated_at = now(),
                provider = COALESCE(#{provider}, provider),
                model = COALESCE(#{model}, model)
            WHERE id = #{id}
            """)
    int touch(@Param("id") String id,
              @Param("provider") String provider,
              @Param("model") String model);

    /**
     * 写入滚动摘要与它覆盖到的消息 id。
     *
     * <p>刻意<b>不动 updated_at</b>：摘要是后台补算的，不该把会话顶到列表最前面 ——
     * 那会让「最近活跃」排序变成「最近被摘要过」，用户会看到顺序自己乱跳。
     */
    @Update("""
            UPDATE conversations SET summary = #{summary}, summary_upto = #{upto}
            WHERE id = #{id}
            """)
    int updateSummary(@Param("id") String id,
                      @Param("summary") String summary,
                      @Param("upto") long upto);
}
