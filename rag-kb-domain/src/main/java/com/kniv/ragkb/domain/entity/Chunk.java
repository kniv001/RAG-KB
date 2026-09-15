package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * 文档分块与它的向量。
 *
 * <p>{@code autoResultMap = true} 是必须的 —— 没有它，MyBatis-Plus 生成的默认
 * resultMap 不会带上 {@code typeHandler}，查询时 embedding 列会被当成普通字符串，
 * 映射到 float[] 就报类型错。这是用自定义 TypeHandler 时最容易漏的一步。
 */
@Data
@TableName(value = "chunks", autoResultMap = true)
public class Chunk {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String docId;

    private Integer seq;

    private String content;

    /** vector(1024)，通过 VectorTypeHandler 以字面量形式读写 */
    @TableField(typeHandler = VectorTypeHandler.class)
    private float[] embedding;

    /** 生成该向量时使用的模型标识，形如 local:bge-m3 */
    private String embedModel;

    private OffsetDateTime createdAt;
}
