package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.MemoryItem;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
public interface MemoryItemMapper extends BaseMapper<MemoryItem> {

    /** 全部记忆，**按最近更新的排前面** —— 超预算时截尾丢的是最老的。 */
    @Select("SELECT * FROM memory_items ORDER BY updated_at DESC, topic")
    List<MemoryItem> listAll();

    /**
     * 覆盖同主题那一条。
     *
     * <p>刻意**只改 item 与 updated_at**：`src_conv` 留着**最早**写下这条的会话，
     * 因为那才是"从哪来的"；后来只是复述它的会话不是来源。
     */
    @Update("""
            UPDATE memory_items SET item = #{item}, updated_at = now()
            WHERE topic = #{topic}
            """)
    int overwrite(@Param("topic") String topic, @Param("item") String item);
}
