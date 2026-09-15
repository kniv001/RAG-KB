package com.kniv.ragkb.service.index;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.IndexTaskMapper;
import com.kniv.ragkb.domain.entity.IndexTask;
import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Service;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 后台索引任务。
 *
 * <p><b>为什么必须异步</b>：Cloudflare 免费版对源站响应有 100 秒硬超时且不可延长，
 * 而大文档的解析 + 向量化很容易超过。改成「立即返回 202 + 任务 id，前端轮询」后，
 * 耗时就与超时无关了。任务状态落库（index_tasks），刷新页面、重启应用都不丢进度。
 *
 * <p>用显式线程池而不是 {@code @Async}：`@Async` 靠代理生效，同类内部调用会静默失效
 * （不报错、只是变成同步执行），而这个失效模式很难发现。显式线程池没这个问题。
 */
@Slf4j
@Service
@RequiredArgsConstructor
@Order(10)
public class IndexTaskService implements ApplicationRunner {

    /** 并发上限 2：本地模型是串行推理，起更多线程只会互相排队并挤爆显存 */
    private final ExecutorService pool = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "index-task");
        t.setDaemon(true);
        return t;
    });

    private final IndexTaskMapper tasks;
    private final IndexService indexService;
    private final ObjectMapper mapper;

    @PostConstruct
    public void init() {
        log.info("索引任务线程池已就绪（并发 2）");
    }

    /**
     * 进程重启后，库里残留的 running 任务其实已经死了 —— 启动时标成中断，
     * 否则前端会看到永远转圈的幽灵任务。
     */
    @Override
    public void run(ApplicationArguments args) {
        try {
            int n = tasks.markRunningAsInterrupted("应用重启，任务中断");
            if (n > 0) {
                log.warn("已把 {} 个残留的 running 索引任务标记为中断", n);
            }
        } catch (Exception e) {
            log.warn("清理残留索引任务失败：{}", e.getMessage());
        }
    }

    public String submit(String docId, Path storageRoot) {
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        IndexTask task = new IndexTask();
        task.setId(id);
        task.setDocId(docId);
        task.setKind("index");
        task.setStatus(IndexTask.STATUS_RUNNING);
        task.setProgress(0);
        task.setTotal(0);
        task.setMessage("已排队");
        tasks.insert(task);

        pool.submit(() -> execute(id, docId, storageRoot));
        return id;
    }

    private void execute(String taskId, String docId, Path storageRoot) {
        long t0 = System.currentTimeMillis();
        try {
            IndexService.IndexResult result = indexService.index(docId, storageRoot,
                    (done, total, message) -> update(taskId, done, total, message));
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("name", result.name());
            data.put("chars", result.chars());
            data.put("chunks", result.chunks());
            data.put("embedModel", result.embedModel());
            data.put("dim", result.dim());
            data.put("elapsedMs", System.currentTimeMillis() - t0);
            update(taskId, result.chunks(), result.chunks(),
                    "完成：" + result.chunks() + " 块");
            finish(taskId, IndexTask.STATUS_DONE, data);
            log.info("索引完成 doc={} chunks={} 耗时 {}ms",
                    docId, result.chunks(), System.currentTimeMillis() - t0);
        } catch (Exception e) {
            log.error("索引失败 doc={}：{}", docId, e.getMessage(), e);
            markError(taskId, e);
            // 失败时把分块清掉、状态退回 stored：宁可显示「未索引」，
            // 也不要留下一半新一半旧的残缺索引 —— 那种状态检索结果不可信且极难察觉
            indexService.clearIndex(docId);
        }
    }

    private void update(String taskId, int done, int total, String message) {
        IndexTask patch = new IndexTask();
        patch.setId(taskId);
        patch.setProgress(done);
        patch.setTotal(total);
        patch.setMessage(message);
        tasks.updateById(patch);
    }

    private void finish(String taskId, String status, Map<String, Object> result) {
        IndexTask patch = new IndexTask();
        patch.setId(taskId);
        patch.setStatus(status);
        try {
            patch.setResult(mapper.writeValueAsString(result));
        } catch (Exception e) {
            patch.setResult("{}");
        }
        tasks.updateById(patch);
    }

    private void markError(String taskId, Exception e) {
        IndexTask patch = new IndexTask();
        patch.setId(taskId);
        patch.setStatus(IndexTask.STATUS_ERROR);
        patch.setMessage(e.getClass().getSimpleName() + ": " + e.getMessage());
        tasks.updateById(patch);
    }

    public IndexTask get(String taskId) {
        return tasks.selectById(taskId);
    }

    public IndexTask latestForDoc(String docId) {
        return tasks.latestForDoc(docId);
    }
}
