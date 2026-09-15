package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.dao.mapper.ChunkMapper;
import com.kniv.ragkb.dao.mapper.DocumentMapper;
import com.kniv.ragkb.domain.entity.Document;
import com.kniv.ragkb.domain.entity.IndexTask;
import com.kniv.ragkb.service.config.StorageProperties;
import com.kniv.ragkb.service.index.IndexService;
import com.kniv.ragkb.service.index.IndexTaskService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.OffsetDateTime;
import java.util.*;

/**
 * 文档管理：上传、列表、建索引、任务轮询、删除。
 *
 * <p>建索引走异步：立即返回 202 + 任务 id，前端轮询进度。
 * 原因是 Cloudflare 免费版对源站响应有 100 秒硬超时且不可延长，
 * 而大文档的解析加向量化很容易超过 —— 同步接口必被掐断。
 */
@Slf4j
@RestController
@RequestMapping("/api/docs")
@RequiredArgsConstructor
public class DocumentController {

    private static final Set<String> ALLOWED =
            Set.of(".txt", ".md", ".markdown", ".pdf", ".docx", ".csv", ".json", ".html", ".htm");

    private final DocumentMapper documents;
    private final ChunkMapper chunks;
    private final IndexService indexService;
    private final IndexTaskService tasks;
    private final StorageProperties storage;

    // ---------------- 上传 ----------------

    @PostMapping("/upload")
    public R<Map<String, Object>> upload(@RequestParam("file") MultipartFile file) throws IOException {
        String name = file.getOriginalFilename() == null ? "" : Path.of(file.getOriginalFilename())
                .getFileName().toString();
        if (name.isBlank()) {
            return R.fail(R.CODE_BAD_REQUEST, "缺少文件名");
        }
        String lower = name.toLowerCase();
        String suffix = lower.contains(".") ? lower.substring(lower.lastIndexOf('.')) : "";
        if (!ALLOWED.contains(suffix)) {
            return R.fail(R.CODE_BAD_REQUEST, "不支持的格式 " + suffix + "，允许：" + ALLOWED);
        }
        long limit = (long) storage.getMaxUploadMb() * 1024 * 1024;
        if (file.getSize() > limit) {
            return R.fail(R.CODE_BAD_REQUEST, "超过上限 " + storage.getMaxUploadMb() + " MB");
        }

        String docId = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        Path dest = storage.uploads().resolve(docId + suffix);
        try (InputStream in = file.getInputStream()) {
            Files.copy(in, dest, StandardCopyOption.REPLACE_EXISTING);
        }

        Document doc = new Document();
        doc.setId(docId);
        doc.setName(name);
        doc.setSuffix(suffix);
        doc.setBytes(file.getSize());
        doc.setStoredPath(storage.getRoot().toAbsolutePath().normalize()
                .relativize(dest).toString().replace('\\', '/'));
        doc.setStatus("stored");
        doc.setChunkCount(0);
        doc.setEmbedModel("");
        documents.insert(doc);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", docId);
        out.put("name", name);
        out.put("bytes", file.getSize());
        out.put("status", "stored");
        return R.ok(out);
    }

    // ---------------- 列表 ----------------

    @GetMapping
    public R<Map<String, Object>> list() {
        List<Document> docs = documents.selectList(null);
        List<Map<String, Object>> rows = new ArrayList<>(docs.size());
        for (Document d : docs) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", d.getId());
            m.put("name", d.getName());
            m.put("suffix", d.getSuffix());
            m.put("bytes", d.getBytes());
            m.put("status", d.getStatus());
            m.put("chunkCount", d.getChunkCount());
            m.put("embedModel", d.getEmbedModel());
            m.put("uploadedAt", fmt(d.getUploadedAt()));
            rows.add(m);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("count", rows.size());
        out.put("docs", rows);
        // 换过向量模型后旧索引全部失效，但它们在库里看起来完全正常 ——
        // 把这类文档单独列出来，否则会表现为「检索突然什么都搜不到」
        out.put("stale", indexService.staleDocuments().stream().map(Document::getId).toList());
        return R.ok(out);
    }

    @GetMapping("/{docId}/chunks")
    public R<Map<String, Object>> chunks(@PathVariable String docId,
                                         @RequestParam(defaultValue = "20") int limit) {
        List<Map<String, Object>> rows = new ArrayList<>();
        chunks.selectList(new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<com.kniv.ragkb.domain.entity.Chunk>()
                        .eq("doc_id", docId).orderByAsc("seq").last("limit " + Math.max(1, Math.min(limit, 200))))
                .forEach(c -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("seq", c.getSeq());
                    m.put("chars", c.getContent() == null ? 0 : c.getContent().length());
                    m.put("preview", c.getContent() == null ? "" :
                            c.getContent().substring(0, Math.min(200, c.getContent().length())));
                    m.put("embedModel", c.getEmbedModel());
                    rows.add(m);
                });
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("docId", docId);
        out.put("total", chunks.countByDoc(docId));
        out.put("chunks", rows);
        return R.ok(out);
    }

    // ---------------- 建索引（异步） ----------------

    @PostMapping("/{docId}/index")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public R<Map<String, Object>> index(@PathVariable String docId) {
        if (documents.selectById(docId) == null) {
            return R.fail(R.CODE_NOT_FOUND, "文档不存在");
        }
        String taskId = tasks.submit(docId, storage.getRoot());
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("taskId", taskId);
        out.put("docId", docId);
        out.put("accepted", true);
        out.put("poll", "/api/docs/tasks/" + taskId);
        return R.ok(out);
    }

    @GetMapping("/tasks/{taskId}")
    public R<Map<String, Object>> task(@PathVariable String taskId) {
        IndexTask t = tasks.get(taskId);
        if (t == null) {
            return R.fail(R.CODE_NOT_FOUND, "任务不存在");
        }
        return R.ok(taskView(t));
    }

    @GetMapping("/{docId}/task")
    public R<Map<String, Object>> latestTask(@PathVariable String docId) {
        IndexTask t = tasks.latestForDoc(docId);
        return R.ok(t == null ? Map.of() : taskView(t));
    }

    // ---------------- 删除 ----------------

    @DeleteMapping("/{docId}")
    public R<Map<String, Object>> delete(@PathVariable String docId) {
        Document d = documents.selectById(docId);
        if (d == null) {
            return R.fail(R.CODE_NOT_FOUND, "文档不存在");
        }
        try {
            Files.deleteIfExists(storage.resolve(d.getStoredPath()));
        } catch (IOException e) {
            log.warn("删除文件失败（继续删库记录）：{}", e.getMessage());
        }
        // chunks 有 ON DELETE CASCADE，跟着文档一起走
        documents.deleteById(docId);
        return R.ok(Map.of("removed", docId));
    }

    // ---------------- 内部 ----------------

    private Map<String, Object> taskView(IndexTask t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", t.getId());
        m.put("docId", t.getDocId());
        m.put("status", t.getStatus());
        m.put("progress", t.getProgress());
        m.put("total", t.getTotal());
        m.put("message", t.getMessage());
        m.put("updatedAt", fmt(t.getUpdatedAt()));
        if (t.getResult() != null && !t.getResult().isBlank()) {
            try {
                m.put("result", new com.fasterxml.jackson.databind.ObjectMapper()
                        .readTree(t.getResult()));
            } catch (Exception ignored) {
                // 结果解析失败不影响任务状态本身
            }
        }
        return m;
    }

    private static String fmt(OffsetDateTime t) {
        return t == null ? null : t.toString();
    }
}
