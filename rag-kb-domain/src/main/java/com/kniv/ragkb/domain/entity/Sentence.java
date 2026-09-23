package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * **句子级索引的一行**：一块被切成若干句，每句带在原文里的字符区间。
 *
 * <p><b>为什么要有它</b>（2026-09-23）：把思考按句切开量过 —— 中位 <b>38%</b> 的字
 * 是在复述<b>已经在提示词里</b>的资料（{@code tools/think-structure-read.py}）。
 * 而"禁止复述"那条提示词**单独试过、不通**：思考反而变长
 * （符号检验 p=0.007，方向是反的）—— 因为**只禁止、不给替代**，模型只会换个方式
 * 做同一件事。
 *
 * <p>这张表提供的就是那个**替代**：句子有了地址，模型可以写
 * {@code ⟨2.4⟩ 说漏桶限制速率} 而不必把原句抄一遍。
 *
 * <p><b>为什么存字符区间而不是只存文本</b>：将来若要按地址把原句拼进正文，
 * 必须**逐字原样**取出。再切一遍就可能与当初切的不一致 ——
 * 而本项目吃过"按位置对齐的东西随重建静默错位"的亏（向量缓存那次 661 条错 438 条）。
 *
 * <p><b>地址的形态</b>：注入提示词时用 {@code ⟨块号.句号⟩}（如 {@code ⟨2.4⟩}），
 * 刻意<b>不用</b> {@code [2.4]} —— 方括号是**引用契约**的语法
 * （判分的正则 {@code \[(\d{1,2})\]} 只认块号），两者混用会让"有效的引用在判据眼里变成缺引用"。
 */
@Data
@TableName("sentences")
public class Sentence {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long chunkId;

    private String docId;

    /** 块内第几句，1 起 —— 地址的后半段就是它。 */
    private Integer seq;

    private Integer charStart;

    private Integer charEnd;

    /** {@code sent} / {@code table} / {@code code} / {@code head} —— 切分时的判定结果。 */
    private String kind;

    private String text;

    /** {@code sha1(去空白后的正文)[:12]} —— **按内容锚定**，不靠位置。 */
    private String sentHash;

    /** 建这份索引时的**语料戳**；与当前语料戳不一致即为陈旧。 */
    private String stamp;

    private OffsetDateTime builtAt;
}
