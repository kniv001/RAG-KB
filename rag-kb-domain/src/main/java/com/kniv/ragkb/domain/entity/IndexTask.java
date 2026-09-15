package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * 后台索引任务。
 *
 * <p>为什么要落库：索引大文档可能跑几分钟，而 Cloudflare 免费版对源站响应有 100 秒
 * 硬超时 —— 同步接口必被掐断。改成「立即返回 202 + 任务 id，前端轮询」后，
 * 耗时就与超时无关了；状态存库则保证刷新页面、重启应用都不丢进度。
 */
@Data
@TableName("index_tasks")
public class IndexTask {

    public static final String STATUS_RUNNING = "running";
    public static final String STATUS_DONE = "done";
    public static final String STATUS_ERROR = "error";

    @TableId(type = IdType.INPUT)
    private String id;

    private String docId;

    private String kind;

    private String status;

    private Integer progress;

    private Integer total;

    private String message;

    /** jsonb：完成后的结果（块数、维度、耗时等） */
    private String result;

    private OffsetDateTime createdAt;

    private OffsetDateTime updatedAt;
}
