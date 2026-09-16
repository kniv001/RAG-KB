package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * 主题树上的一个节点。
 *
 * <p>只做两层（主题簇），不做深树：深树的收益来自「逐层收窄」，那需要每下降
 * 一层付一次 KV，在 10240 的上下文预算下不划算；两层的收益（全局概览 + 粗筛）
 * 已经拿到了绝大部分。
 *
 * <p>{@code chunkIds} 是该簇覆盖的块。检索时据此把搜索范围收窄到相关主题，
 * 这是「先粗后细」里「粗」那一步的实际作用。
 */
@Data
@TableName(value = "tree_nodes", autoResultMap = true)
public class TreeNode {

    @TableId(type = IdType.INPUT)
    private String id;

    /** 主题名，几个字，用于概览 */
    private String label;

    /** 该主题覆盖了哪些内容，一两句话 */
    private String summary;

    /**
     * 本簇覆盖的块 id。
     *
     * <p>PostgreSQL 的 {@code bigint[]} 驱动默认映射不了，必须显式给 TypeHandler，
     * 否则写入时报类型错 —— 与 vector / jsonb 是同一类问题。
     */
    @TableField(typeHandler = com.kniv.ragkb.domain.handler.LongArrayTypeHandler.class)
    private Long[] chunkIds;

    @TableField(typeHandler = com.kniv.ragkb.domain.handler.TextArrayTypeHandler.class)
    private String[] docIds;

    /** 簇的质心。检索时拿问题向量与它比距离，判断问题落在哪个主题 */
    @TableField(typeHandler = com.kniv.ragkb.domain.handler.VectorTypeHandler.class)
    private float[] centroid;

    /** 覆盖多少块 */
    private Integer size;

    private OffsetDateTime builtAt;
}
