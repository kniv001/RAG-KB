package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.Message;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

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
}
