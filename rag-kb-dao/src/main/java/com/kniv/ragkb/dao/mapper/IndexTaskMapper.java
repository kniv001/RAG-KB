package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.IndexTask;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
public interface IndexTaskMapper extends BaseMapper<IndexTask> {

    @Select("""
            SELECT * FROM index_tasks WHERE doc_id = #{docId}
            ORDER BY created_at DESC LIMIT 1
            """)
    IndexTask latestForDoc(@Param("docId") String docId);

    @Select("SELECT * FROM index_tasks WHERE status = 'running' ORDER BY created_at DESC")
    List<IndexTask> listRunning();

    @Update("""
            UPDATE index_tasks
            SET status = 'error', message = #{message}, updated_at = now()
            WHERE status = 'running'
            """)
    int markRunningAsInterrupted(@Param("message") String message);
}
