package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
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
@TableName("messages")
public class Message {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String convId;

    /** user / assistant */
    private String role;

    private String content;

    /** jsonb，这里是原始 JSON 字符串；需要结构化时由 service 层解析 */
    private String sources;

    private String provider;

    private String model;

    private OffsetDateTime createdAt;
}
