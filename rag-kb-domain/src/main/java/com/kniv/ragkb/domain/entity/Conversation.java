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

    /**
     * 会话滚动摘要，由 {@link com.kniv.ragkb.service.chat.SummaryService 摘要服务}增量维护。
     *
     * <p>它是历史索引的兜底：向量检索漏召时，至少还有一份覆盖全部的粗粒度背景。
     * 为空表示还没生成过。
     */
    private String summary;

    /**
     * 摘要已覆盖到的消息 id（{@code messages.id}）。0 表示一条都没覆盖。
     *
     * <p>增量更新的锚点：下次只把「id 大于它、且已经掉出最近窗口」的那些消息
     * 并入摘要，不必重读整个会话。
     */
    private Long summaryUpto;
}
