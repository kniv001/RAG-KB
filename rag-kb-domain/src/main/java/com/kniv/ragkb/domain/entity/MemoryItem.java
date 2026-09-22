package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * **长期记忆的一条** —— 跨会话存活的那类事实（偏好、约定、已经定下来的决定）。
 *
 * <p><b>为什么要有它</b>：在此之前，"记忆"只有 {@code conversations.summary}
 * —— **按会话隔离**。这次对话里说定的「以后都要给我改动文件清单」，换个会话就没了。
 * 而 2026-09-18 那次「记忆当检索」的实验已经证明这条路可行：
 * 一行一条的索引 + 按需召回，**11/11 正确、只占全量上下文的 23%**
 * （而全量根本装不进窗口）。
 *
 * <p><b>与会话摘要的分工</b>（不是替代关系）：
 * <ul>
 *   <li>会话摘要 = <b>本次对话的上下文</b>（指代、刚才说了什么）—— 按会话，会过时</li>
 *   <li>长期记忆 = <b>跨会话的持久事实</b> —— 全局，一条主题只留一行</li>
 * </ul>
 * 两者会重叠（同一句话在两边都出现），这是**故意**的：重叠时两边说的是同一件事，
 * 代价只是几个 token；而漏掉一边的代价是"它忘了"。
 *
 * <p><b>形状沿用摘要那一套</b>（{@code 主题：曾经 → 现在}），不是新发明 ——
 * 于是 `摘要服务`的机械件（解析、着落判据、覆盖边、只增不删）**原样可用**。
 */
@Data
@TableName("memory_items")
public class MemoryItem {

    /** 主题即主键 —— **一条主题只留一行**，覆盖时改这一行而不是追加。 */
    @TableId(type = IdType.INPUT)
    private String topic;

    /** 整行原文：`主题：曾经 → 现在`。 */
    private String item;

    /** 来自哪个会话 —— 出问题时能追回去。 */
    private String srcConv;

    private OffsetDateTime updatedAt;
}
