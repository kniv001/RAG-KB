package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/** 会话。provider/model 记录该会话最后使用的模型，便于回溯「这个回答是谁生成的」。 */
@Data
@TableName("conversations")
public class Conversation {

    @TableId(type = IdType.INPUT)
    private String id;

    private String title;

    private String provider;

    private String model;

    private OffsetDateTime createdAt;

    private OffsetDateTime updatedAt;
}
