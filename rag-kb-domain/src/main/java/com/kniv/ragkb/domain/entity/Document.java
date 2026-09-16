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

    /** upload = 用户自己传的；web = 联网抓回来的 */
    private String sourceKind;

    /** 抓取来源的网址，仅 sourceKind=web 时有值 */
    private String sourceUrl;

    /**
     * 网页的抓取时间。
     *
     * <p>必须记：网页内容会过期。同一条结论，三年前抓的和昨天抓的可信度完全不同，
     * 而入库之后两者在检索结果里长得一模一样。引用网页来源时要带上这个时间，
     * 否则会把陈旧结论当成现行事实。
     */
    private OffsetDateTime fetchedAt;
}
