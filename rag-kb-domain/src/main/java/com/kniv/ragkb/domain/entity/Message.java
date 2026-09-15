package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import com.kniv.ragkb.domain.handler.JsonbTypeHandler;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * 一条消息。
 *
 * <p>{@code sources} 是 jsonb，存当轮召回的来源（文档名 / 块号 / 相似度 / 摘要）。
 * 每条助手消息都带上它，才能事后回答「这句话是从哪来的」——
 * 没有来源记录的 RAG 回答，可信度无法验证。
 */
@Data
@TableName(value = "messages", autoResultMap = true)
public class Message {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String convId;

    /** user / assistant */
    private String role;

    private String content;

    /**
     * 当轮召回的来源，数据库列是 jsonb，这里是原始 JSON 字符串。
     *
     * <p>必须带 JsonbTypeHandler：不加会以 varchar 发送，而 varchar → jsonb
     * 无隐式转换，写入消息时直接报类型错。
     */
    @com.baomidou.mybatisplus.annotation.TableField(typeHandler = JsonbTypeHandler.class)
    private String sources;

    private String provider;

    private String model;

    /**
     * 历史索引用的向量（vector(1024)），为空表示这条还没索引。
     *
     * <p>用途与 {@code chunks.embedding} 不同：chunks 是知识库文档的分块，检索范围是全局；
     * 这里是<b>对话历史</b>，检索范围限定在本会话内。旧轮次不再因为超出
     * {@code HISTORY_LIMIT} 就被整段丢弃，而是可以被召回。
     *
     * <p>必须带 VectorTypeHandler 且类上标 {@code autoResultMap = true} ——
     * 缺任一个，驱动会按 varchar 发送，写入 vector 列直接报类型错
     * （chunks 表上已经踩过同款）。
     */
    @com.baomidou.mybatisplus.annotation.TableField(typeHandler = VectorTypeHandler.class)
    private float[] embedding;

    /** 生成该向量时用的模型标识，形如 local:bge-m3。不同模型的向量不在同一空间，混查会静默返回垃圾 */
    private String embedModel;

    private OffsetDateTime createdAt;
}
