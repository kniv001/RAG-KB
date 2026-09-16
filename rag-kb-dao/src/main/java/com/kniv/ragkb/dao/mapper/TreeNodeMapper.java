package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.TreeNode;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;
import java.util.Map;

/**
 * 主题树。
 *
 * <p>重建是全量替换（先清空再写入）而不是增量更新：聚类的结果本身是全局的 ——
 * 新加几篇文档可能让簇的边界整体移动，增量更新会得到一个既不是旧结果、
 * 也不是新结果的中间态。全量替换虽然重，但结果是确定的。
 */
@Mapper
public interface TreeNodeMapper extends BaseMapper<TreeNode> {

    @Select("SELECT * FROM tree_nodes ORDER BY size DESC")
    List<TreeNode> listAll();

    @Delete("DELETE FROM tree_nodes")
    int clear();

    /** 建树时的输入：全部块的 id 与向量。只取有向量的，且按当前向量模型过滤。 */
    @Select("""
            SELECT c.id AS id, c.doc_id AS doc_id, d.name AS doc_name,
                   c.content AS content, c.embedding AS vec
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE c.embedding IS NOT NULL AND c.embed_model = #{model}
            ORDER BY c.id
            """)
    List<Map<String, Object>> allVectors(@Param("model") String embedModel);

    @Select("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND embed_model = #{model}")
    int countIndexed(@Param("model") String embedModel);

    @Insert("""
            INSERT INTO tree_meta (id, chunk_count, cluster_count, built_at)
            VALUES (1, #{chunkCount}, #{clusterCount}, now())
            ON CONFLICT (id) DO UPDATE
            SET chunk_count = EXCLUDED.chunk_count,
                cluster_count = EXCLUDED.cluster_count,
                built_at = EXCLUDED.built_at
            """)
    int saveMeta(@Param("chunkCount") int chunkCount, @Param("clusterCount") int clusterCount);

    @Select("SELECT chunk_count, cluster_count, built_at FROM tree_meta WHERE id = 1")
    Map<String, Object> meta();
}
