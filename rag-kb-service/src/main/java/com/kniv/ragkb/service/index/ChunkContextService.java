package com.kniv.ragkb.service.index;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.JsonExtract;
import com.kniv.ragkb.service.cache.CacheService;
import com.kniv.ragkb.service.config.GpuGate;
import com.kniv.ragkb.service.config.RagProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

/**
 * 语境行：给每块生成一句「**这段能回答什么问题**」，只进检索索引、不进提示词。
 *
 * <p><b>为什么需要</b>：2026-09-18 的难题集（`tools/hard-query-probe.py`）测出真失败 ——
 * 问句用的是**症状词**（「怎么让服务固定在同一台机器上」），文档用的是**机制词**
 * （「节点亲和性」「调度」「标签」），两者在向量空间里够不着：那条的靶子排在 **rank 50**，
 * 加宽召回也没用（要提到 50 才行）。
 *
 * <p>给每块加一句用提问者说法写的语境行再嵌入之后（`tools/contextual-retrieval-probe.py`）：
 * 靶子回到 **rank 28**，难题 **13/14 → 14/14**。免费的办法试过了 —— 只把文档标题前置
 * **完全没用**（标题带的还是机制词），语境行必须让模型写。
 *
 * <p><b>提示词的措辞是实验定的</b>，两处不能改：
 * <ul>
 *   <li>问「能回答什么」而不是「讲了什么」—— 后者只会复述文档术语（精度上去、硬失败没救）</li>
 *   <li>必须禁止元信息 —— 实测「本文由某某撰写」「抓取时间」这类行占 9%，
 *       对检索零价值还占着索引位置</li>
 * </ul>
 *
 * <p><b>为什么只进索引</b>：回答里展示与引用的仍是 content 原文。逐字可追溯是这套系统
 * 唯一的信任边界，不能被索引层的改写污染 —— 语境行只影响"能不能被召回"。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChunkContextService {

    private static final String PROMPT = """
            你在给检索索引加语境。下面是一篇文档的标题和一个片段。
            请写一句话：**这段内容能回答什么问题**。

            要求：
            1. **用提问者的说法**写 —— 想查这件事的人会怎么问，就用那些词。
               不要用文档里的术语（术语往往正是提问者不知道的东西）。
            2. **禁止元信息**：不许写「本文由…撰写」「文章来源」「抓取时间」「作者是谁」这类内容。
            3. 不许只写省略号或空话。
            4. 不超过 40 字。

            只输出 JSON：{"q":"..."}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}""";

    private final ProviderRegistry providers;
    private final RagProperties props;
    private final CacheService cache;
    private final ObjectMapper mapper;
    private final GpuGate gpuGate;

    /**
     * 逐块生成（失败给空串，索引照建，只是这块少了语境行）。结果按
     * 「文档名 + 块文本」哈希缓存 —— 重传同一文件或重建索引时几乎瞬间完成。
     */
    public List<String> contextsFor(String docName, List<String> pieces) {
        if (!props.getContext().isEnabled()) {
            return pieces.stream().map(p -> "").toList();
        }
        // 语境行是 1 块 1 次调用（嵌入是 16 块 1 次），抢 GPU 的量级完全不同，
        // 先让一次；拿不到也照跑 —— 索引必须建完，慢一点好过不建。
        if (!gpuGate.awaitIdle()) {
            log.info("语境行生成：未等到 GPU 空闲，直接开始（共 {} 块）", pieces.size());
        }
        ProviderRegistry.Ref ref = providers.resolveChat(props.getAgent().getUtilityModel());
        List<String> out = new ArrayList<>(pieces.size());
        int gen = 0;
        for (int i = 0; i < pieces.size(); i++) {
            String piece = pieces.get(i);
            String key = CacheService.hash("ctx", docName, piece);
            String hit = cache.getParse(key);
            if (hit != null) {
                out.add(hit);
                continue;
            }
            String ctx = one(ref, docName, i, piece);
            cache.putParse(key, "ctx:" + docName, ctx);
            out.add(ctx);
            gen++;
        }
        log.debug("语境行：{} 块，新生成 {}，其余命中缓存", pieces.size(), gen);
        return out;
    }

    private String one(ProviderRegistry.Ref ref, String docName, int seq, String piece) {
        try {
            String user = "标题：《" + docName + "》\n位置：第 " + seq + " 块\n片段：\n" + piece;
            String reply = providers.chatJson(ref,
                    List.of(ChatMessage.system(PROMPT), ChatMessage.user(user)),
                    0.2, SCHEMA).content();
            JsonNode node = JsonExtract.parseObject(mapper, reply);
            String q = node == null ? "" : JsonExtract.string(node, "q", "");
            q = q == null ? "" : q.strip();
            // 退化解：只写省略号/空话的，当作没生成（实测这类占 9%）
            if (q.length() < 6 || q.chars().allMatch(c -> c == '.' || c == '…' || c == '。')) {
                return "";
            }
            return q;
        } catch (Exception e) {
            log.debug("语境行生成失败（第 {} 块）：{}", seq, e.getMessage());
            return "";
        }
    }
}
