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
}
