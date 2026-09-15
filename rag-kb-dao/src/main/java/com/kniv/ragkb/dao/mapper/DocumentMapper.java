package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.Document;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
public interface DocumentMapper extends BaseMapper<Document> {

    /**
     * 已索引、但用的不是当前向量模型的文档 —— 需要重建索引。
     *
     * <p>这个查询是「换向量模型」这个操作的安全网：模型一换，旧向量全部失效，
     * 但它们在库里看起来完全正常。不做这个检查就会出现「检索突然什么都搜不到，
     * 而所有文档都显示已索引」这种最难排查的状态。
     */
    @Select("""
            SELECT * FROM documents
            WHERE chunk_count > 0 AND embed_model <> #{current}
            ORDER BY uploaded_at
            """)
    List<Document> findStale(@Param("current") String currentEmbedModel);

    @Update("""
            UPDATE documents
            SET chunk_count = #{count}, status = #{status}, embed_model = #{model}
            WHERE id = #{id}
            """)
    int updateIndexState(@Param("id") String id,
                         @Param("count") int chunkCount,
                         @Param("status") String status,
                         @Param("model") String embedModel);
}
