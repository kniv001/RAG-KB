package com.kniv.ragkb.service.chat;

import com.kniv.ragkb.dao.mapper.MessageMapper;
import com.kniv.ragkb.domain.entity.Message;
import com.kniv.ragkb.domain.handler.VectorTypeHandler;
import com.kniv.ragkb.service.config.RagProperties;
import com.kniv.ragkb.service.index.EmbeddingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * 会话历史的向量索引 —— 让旧轮次可被召回，而不是超出窗口就整段丢弃。
 *
 * <p><b>为什么值得做</b>：KV 缓存是每 token 约 147 KB，而向量是每 600 token 才 4 KB，
 * 相差约 <b>2.2 万倍</b>。100 轮对话全量进上下文要 6.2 GB 显存（8GB 卡物理上不可能），
 * 而索引只要 0.3 MB。
 *
 * <p><b>省的是哪一部分</b>：召回回来的那几轮<b>照样付全额的 KV</b>。
 * 省下的是「没被召回的那绝大部分」—— 只为这一轮真正需要的内容付费。
 *
 * <p><b>与知识库检索的区别</b>：{@code chunks} 是全局知识库，这里是单个会话的历史。
 * 两者刻意分开查：混在一起会让「上次我们聊到哪」被无关文档淹没。
 *
 * <p><b>已知的失败模式</b>：检索漏召 = 该轮次永久丢失。指代和省略尤其致命 ——
 * 用户问「那它呢」，向量检索很可能召不回定义「它」的那一轮，因为那轮的文本里
 * 根本没有「它」这个词。所以这个方法只做<b>增量</b>：它在现有窗口之外补充，
 * 不替换现有窗口。窗口内的近轮次仍然是原文全文，不受检索质量影响。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class HistoryIndexService {

    private final MessageMapper messages;
    private final EmbeddingService embedding;
    private final RagProperties props;

    /** 召回结果。text 直接进提示词，ids 用于排查召回了哪些 */
    public record Excerpt(String text, List<Long> ids) {
        public static final Excerpt EMPTY = new Excerpt("", List.of());

        public boolean isEmpty() {
            return text == null || text.isBlank();
        }
    }

    /**
     * 增量补索引：把这个会话里还没有向量的消息补上。
     *
     * <p>只做增量而不是每轮重算整个会话：向量化是这条链上最贵的一步，
     * 而历史只会追加、不会改写。
     *
     * <p>刻意同步执行而不是丢线程池：两条新消息一次批量向量化约 30~50ms
     * （实测 bge-m3 单次往返 8~56ms），不值得为它引入线程池与状态管理。
     * 真变慢了再挪。
     *
     * @return 本次索引的条数
     */
    public int indexPending(String convId) {
        if (!props.getHistory().isEnabled() || convId == null || convId.isBlank()) {
            return 0;
        }
        List<Message> pending = messages.pendingIndex(convId, props.getHistory().getIndexBatch());
        if (pending.isEmpty()) {
            return 0;
        }
        List<String> texts = new ArrayList<>(pending.size());
        for (Message m : pending) {
            texts.add(m.getContent());
        }
        try {
            List<float[]> vectors = embedding.embedBatched(texts, 16, null);
            String model = embedding.modelColumn();
            int done = 0;
            for (int i = 0; i < pending.size(); i++) {
                messages.setEmbedding(pending.get(i).getId(),
                        VectorTypeHandler.toLiteral(vectors.get(i)), model);
                done++;
            }
            log.debug("会话 {} 补索引 {} 条", convId, done);
            return done;
        } catch (Exception e) {
            // 索引失败绝不能让问答挂掉：它只是锦上添花，主链路不依赖它
            log.warn("历史索引失败（不影响本轮回答）", e);
            return 0;
        }
    }

    /**
     * 召回与本轮问题相关的旧轮次。
     *
     * @param excludeIds 已作为「最近历史」进入提示词的消息 id —— 它们已经在上下文里了，
     *                   再召回一遍是纯浪费
     */
    public Excerpt retrieve(String convId, String question, Set<Long> excludeIds) {
        RagProperties.History cfg = props.getHistory();
        if (!cfg.isEnabled() || convId == null || convId.isBlank()
                || question == null || question.isBlank()) {
            return Excerpt.EMPTY;
        }
        try {
            float[] q = embedding.embedOne(question);
            // 排除下推到 SQL：先取后过滤的话，取回来的很可能全在排除集里
            // （最近窗口那几条永远是最相似的），过滤完就空了。
            Long[] exclude = excludeIds == null ? new Long[0] : excludeIds.toArray(new Long[0]);
            List<Message> hits = messages.searchByVector(convId,
                    VectorTypeHandler.toLiteral(q), embedding.modelColumn(), exclude, cfg.getTopK());

            List<Long> picked = new ArrayList<>();
            for (Message m : hits) {
                if (m.getId() != null) {
                    picked.add(m.getId());
                }
            }
            if (picked.isEmpty()) {
                return Excerpt.EMPTY;
            }

            // 连同前后各一条一起取：一轮对话由相邻两条组成，只给命中的那条，
            // 模型会看到一段没有来由的回答
            List<Message> expanded = messages.withNeighbors(convId, picked.toArray(new Long[0]));

            StringBuilder sb = new StringBuilder();
            List<Long> used = new ArrayList<>();
            for (Message m : expanded) {
                if (m.getContent() == null || m.getContent().isBlank()) {
                    continue;
                }
                // 有笔记就用笔记：它是改写成自包含的版本 —— 没有指代、没有
                // 「根据参考资料[1]」这类包装，比原文更适合给模型读。
                // 笔记只存在于窗口之外的轮次，所以不会和提示词里的原文重复。
                String body = m.getIndexText() != null && !m.getIndexText().isBlank()
                        ? m.getIndexText()
                        : ("assistant".equals(m.getRole()) ? "助手：" : "用户：") + m.getContent();
                String line = body.replace('\n', ' ').strip();
                if (sb.length() + line.length() > cfg.getExcerptChars()) {
                    break;   // 预算用尽就停，不硬塞
                }
                sb.append(line).append('\n');
                used.add(m.getId());
            }
            if (sb.length() == 0) {
                return Excerpt.EMPTY;
            }
            log.debug("会话 {} 召回了 {} 条旧消息", convId, used.size());
            return new Excerpt(sb.toString().strip(), used);
        } catch (Exception e) {
            log.warn("历史召回失败（不影响本轮回答）", e);
            return Excerpt.EMPTY;
        }
    }
}
