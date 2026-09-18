package com.kniv.ragkb.service.index;

import com.kniv.ragkb.dao.mapper.ChunkMapper;
import com.kniv.ragkb.dao.mapper.DocumentMapper;
import com.kniv.ragkb.domain.entity.Chunk;
import com.kniv.ragkb.domain.entity.Document;
import com.kniv.ragkb.service.cache.CacheService;
import com.kniv.ragkb.service.chunk.TextChunker;
import com.kniv.ragkb.service.parse.DocumentParser;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;

/**
 * 索引管线：解析 → 切分 → 向量化 → 入库。
 *
 * <p>全链路带缓存：解析按<b>文件内容哈希</b>缓存（同一文件重传或重建时跳过解析），
 * 向量按<b>文本 + 模型哈希</b>缓存（重建索引时几乎瞬间完成）。
 * 两个缓存都在 {@link CacheService} 里，键含所有影响结果的参数，因此不会返回陈旧数据。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class IndexService {

    /** 向量化分批大小。小模型一次吃太多容易超时或爆内存 */
    private static final int EMBED_BATCH = 16;

    private final DocumentMapper documents;
    private final ChunkMapper chunks;
    private final DocumentParser parser;
    private final TextChunker chunker;
    private final ChunkContextService context;
    private final EmbeddingService embedding;
    private final CacheService cache;

    public static class IndexException extends RuntimeException {
        public IndexException(String message) {
            super(message);
        }

        public IndexException(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /** 进度回调：已完成、总数、当前阶段说明 */
    public interface Progress {
        void report(int done, int total, String message);
    }

    public record IndexResult(String docId, String name, int chars, int chunks,
                              String embedModel, int dim) {
    }

    /**
     * 对一篇已上传的文档建索引。可安全重复调用（整篇替换）。
     */
    @Transactional
    public IndexResult index(String docId, Path storageRoot, Progress progress) {
        Document doc = documents.selectById(docId);
        if (doc == null) {
            throw new IndexException("文档不存在：" + docId);
        }
        Path path = storageRoot.resolve(doc.getStoredPath());
        if (!Files.exists(path)) {
            throw new IndexException("文件缺失：" + path);
        }

        // ---- 1) 解析（按文件内容哈希缓存）----
        report(progress, 0, 0, "计算文件指纹");
        String fileHash = sha256File(path);
        String text = cache.getParse(fileHash);
        if (text == null) {
            report(progress, 0, 0, "解析文档");
            text = parser.parse(path);
            if (!text.isBlank()) {
                cache.putParse(fileHash, doc.getName(), text);
            }
        } else {
            report(progress, 0, 0, "解析缓存命中");
        }
        if (text.isBlank()) {
            throw new IndexException("解析结果为空，可能是扫描版 PDF 或空文件");
        }

        // ---- 2) 切分 ----
        List<String> pieces = chunker.split(text);
        if (pieces.isEmpty()) {
            throw new IndexException("切分后无有效分块（原文 " + text.length() + " 字）");
        }
        report(progress, 0, pieces.size(), "切分为 " + pieces.size() + " 块");

        // ---- 3) 语境行（只进索引，不进提示词）----
        // 难题集实测：不加语境行时"症状词问句"够不着"机制词文档"，靶子排在 rank 50；
        // 加上之后回到 28，难题 13/14 → 14/14。免费的标题前置试过，完全没用。
        report(progress, 0, pieces.size(), "生成语境行");
        List<String> ctxs = context.contextsFor(doc.getName(), pieces);

        // ---- 4) 向量化：嵌「语境行 + 正文」（按文本 + 模型哈希缓存）----
        List<String> embedTexts = new ArrayList<>(pieces.size());
        for (int i = 0; i < pieces.size(); i++) {
            String ctx = ctxs.get(i);
            embedTexts.add(ctx == null || ctx.isBlank() ? pieces.get(i) : ctx + "\n" + pieces.get(i));
        }
        List<float[]> vectors = embedding.embedBatched(embedTexts, EMBED_BATCH,
                (done, total) -> report(progress, done, total, "向量化 " + done + "/" + total));

        // ---- 5) 入库（整篇替换）----
        report(progress, pieces.size(), pieces.size(), "写入数据库");
        String model = embedding.modelColumn();
        chunks.deleteByDoc(docId);
        for (int i = 0; i < pieces.size(); i++) {
            Chunk c = new Chunk();
            c.setDocId(docId);
            c.setSeq(i);
            c.setContent(pieces.get(i));
            c.setCtx(ctxs.get(i));
            c.setEmbedding(vectors.get(i));
            c.setEmbedModel(model);
            chunks.insert(c);
        }
        documents.updateIndexState(docId, pieces.size(), "indexed", model);

        int dim = vectors.isEmpty() ? 0 : vectors.get(0).length;
        return new IndexResult(docId, doc.getName(), text.length(), pieces.size(), model, dim);
    }

    /** 删除一篇文档的全部分块，并把状态退回 stored。 */
    @Transactional
    public void clearIndex(String docId) {
        chunks.deleteByDoc(docId);
        documents.updateIndexState(docId, 0, "stored", "");
    }

    private static void report(Progress p, int done, int total, String message) {
        if (p != null) {
            p.report(done, total, message);
        }
    }

    private static String sha256File(Path path) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] buf = new byte[1 << 20];
            try (InputStream in = Files.newInputStream(path)) {
                int n;
                while ((n = in.read(buf)) > 0) {
                    md.update(buf, 0, n);
                }
            }
            StringBuilder hex = new StringBuilder();
            for (byte b : md.digest()) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (IOException | java.security.NoSuchAlgorithmException e) {
            throw new IndexException("计算文件指纹失败：" + e.getMessage(), e);
        }
    }

    /** 已索引但用的不是当前向量模型的文档 —— 换模型后的安全网。 */
    public List<Document> staleDocuments() {
        return documents.findStale(embedding.modelColumn());
    }

    /** 供文档列表用：把实体的字段整成前端要的形状。 */
    public List<Document> all() {
        List<Document> out = new ArrayList<>(documents.selectList(null));
        return out;
    }
}
