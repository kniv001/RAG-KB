package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import com.kniv.ragkb.domain.handler.JsonbTypeHandler;
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

    private OffsetDateTime createdAt;
}
