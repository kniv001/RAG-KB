package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.dao.mapper.ChunkMapper;
import com.kniv.ragkb.dao.mapper.DocumentMapper;
import com.kniv.ragkb.domain.entity.Document;
import com.kniv.ragkb.domain.entity.IndexTask;
import com.kniv.ragkb.security.crypto.EncryptedApiFilter;
import com.kniv.ragkb.security.crypto.HybridCryptoService;
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
    private final HybridCryptoService crypto;

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

    // ---------------- 加密上传 ----------------

    /**
     * 加密上传：请求体是文件密文本身（{@code application/octet-stream}），
     * 文件名与文件 IV 藏在加密的元信息里。
     *
     * <p><b>为什么另开一个接口而不是改造 multipart</b>：multipart 的分片边界、
     * 头字段、文件名全是明文，中间设备（本场景下是 Cloudflare —— 它终止 TLS，
     * 能读到转发的全部内容）可以完整还原出文件与令牌。而那正是这层加密要防的东西。
     *
     * <p><b>为什么体不走内存</b>：整份读进来解密，一个 50MB 的文件要占 100MB 堆。
     * 这里边读边解密边落盘，峰值只有一个 64KB 的缓冲。
     *
     * <p>先写临时文件、认证标签校验通过后才改名就位 —— 否则一个标签不对的请求
     * 会在磁盘上留下半截文件，而它看起来和正常文件一模一样。
     */
    @PostMapping(value = "/upload-encrypted", consumes = "application/octet-stream")
    public R<Map<String, Object>> uploadEncrypted(jakarta.servlet.http.HttpServletRequest req) {
        byte[] aesKey = EncryptedApiFilter.aesKeyOf(req);
        com.fasterxml.jackson.databind.JsonNode meta = EncryptedApiFilter.metaOf(req);
        if (aesKey == null || meta == null) {
            return R.fail(R.CODE_BAD_REQUEST, "该接口必须走加密上传（application/octet-stream + X-Enc-Meta）");
        }

        String rawName = meta.path("name").asText("");
        String name = rawName.isBlank() ? "" : Path.of(rawName).getFileName().toString();
        String iv = meta.path("iv").asText("");
        long declared = meta.path("size").asLong(0);
        if (name.isBlank() || iv.isBlank()) {
            return R.fail(R.CODE_BAD_REQUEST, "元信息缺少文件名或 IV");
        }

        String lower = name.toLowerCase();
        String suffix = lower.contains(".") ? lower.substring(lower.lastIndexOf('.')) : "";
        if (!ALLOWED.contains(suffix)) {
            return R.fail(R.CODE_BAD_REQUEST, "不支持的格式 " + suffix + "，允许：" + ALLOWED);
        }
        long limit = (long) storage.getMaxUploadMb() * 1024 * 1024;
        if (declared > limit) {
            return R.fail(R.CODE_BAD_REQUEST, "超过上限 " + storage.getMaxUploadMb() + " MB");
        }

        String docId = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        Path dest = storage.uploads().resolve(docId + suffix);
        Path temp = storage.uploads().resolve(docId + suffix + ".part");

        long written;
        try {
            javax.crypto.Cipher cipher = crypto.decryptCipher(aesKey, iv);
            written = 0;
            try (InputStream in = req.getInputStream();
                 java.io.OutputStream out = Files.newOutputStream(temp)) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) {
                    byte[] plain = cipher.update(buf, 0, n);
                    if (plain != null && plain.length > 0) {
                        out.write(plain);
                        written += plain.length;
                    }
                    if (written > limit) {
                        throw new IOException("超过上限 " + storage.getMaxUploadMb() + " MB");
                    }
                }
                byte[] tail = cipher.doFinal();   // 标签不对会在这里抛，文件不会被改名就位
                if (tail != null && tail.length > 0) {
                    out.write(tail);
                    written += tail.length;
                }
            }
            Files.move(temp, dest, StandardCopyOption.REPLACE_EXISTING);
        } catch (Exception e) {
            try { Files.deleteIfExists(temp); } catch (IOException ignored) { /* 尽力清理 */ }
            log.warn("加密上传失败：{}", e.getMessage());
            return R.fail(R.CODE_BAD_REQUEST, "解密或落盘失败：" + e.getMessage());
        }

        Document doc = new Document();
        doc.setId(docId);
        doc.setName(name);
        doc.setSuffix(suffix);
        doc.setBytes(written);
        doc.setStoredPath(storage.getRoot().toAbsolutePath().normalize()
                .relativize(dest).toString().replace('\\', '/'));
        doc.setStatus("stored");
        doc.setChunkCount(0);
        doc.setEmbedModel("");
        documents.insert(doc);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", docId);
        out.put("name", name);
        out.put("bytes", written);
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
