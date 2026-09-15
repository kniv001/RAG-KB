package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * 已上传的文档。
 *
 * <p>表结构与 Python 版一致，两套系统共用一个库。
 * {@code embedModel} 记录这篇文档是用哪个向量模型索引的 —— 换模型后旧向量与新向量
 * 不在同一空间，检索时必须按当前模型过滤，否则会静默返回垃圾结果。
 */
@Data
@TableName("documents")
public class Document {

    @TableId(type = IdType.INPUT)
    private String id;

    private String name;

    private String suffix;

    private Long bytes;

    private String storedPath;

    private OffsetDateTime uploadedAt;

    /** stored / indexed / error */
    private String status;

    private Integer chunkCount;

    /** 形如 local:bge-m3；空串表示尚未索引 */
    private String embedModel;
}
