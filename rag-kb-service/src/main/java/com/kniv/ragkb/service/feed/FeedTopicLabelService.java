package com.kniv.ragkb.service.feed;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.FeedIndexMapper;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.JsonExtract;
import com.kniv.ragkb.service.config.GpuGate;
import com.kniv.ragkb.service.config.RagProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * **给议题起名** —— 让"哪件事在升温"这句问话有个能看的答案。
 *
 * <p>在此之前 `feed_topics.label` **全是 NULL**：议题聚出来了（同一件事的多条报道能被正确并簇，
 * 实测相似度 0.882 那对就是这么并的），但它在界面上只能显示成「议题 56（12 条）」——
 * 而人要看的是「哪件事」。**聚类与"能看"之间差的就是这一次命名。**
 *
 * <p>做法与主题树给簇起名同一套（{@code TreeBuildService#labelFor} 的模式）：
 * 拿成员标题（不读正文 —— 起名不需要读全文）问一次模型，要求给一个**不超过 12 字的
 * 事件事名**，而不是主题词。差别在于：
 * <ul>
 *   <li>树那边是**主题**（"缓存分层设计"这种长期方向）；这边是**事件**（"台风沙德尔登陆"这种一时的事）；</li>
 *   <li>所以提示词明确要求写"谁在什么时候做了什么"，并允许"标题之间看不出是同一件事"时如实标"杂项"。</li>
 * </ul>
 *
 * <p>⚠️ **名字要校验**（沿用树那边的教训）：模型给过 {@code {"label":"..."}} —— 语法合法、
 * 形状正确，于是被当成成功，主题列表里出现一个叫「...」的主题。这里要求名字里
 * 至少有一个实义字符，且不能是纯标点。
 *
 * <p>⚠️ 起名要调模型 ⇒ 调用方负责等 GPU 静默（见 {@code FeedEnrichService} 与轮询里的用法）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class FeedTopicLabelService {

    /** 起名只看标题 —— 事件名写在标题里，读正文是浪费（而且正文可能带着没洗干净的家具）。 */
    private static final String PROMPT = """
            你在给新闻事件起名。下面是一组报道的标题，它们**应该**是同一件事。
            请给这件事起一个名字。

            要求：
            1. 名字**不超过 12 个字**，是**事件的名称**，不是主题词 ——
               写「台风沙德尔登陆浙江」而不是「台风」；写「李强会见马来西亚总理」而不是「外交活动」。
            2. 尽量包含「谁」或「什么地方」，让人一眼知道是哪件事。
            3. 若这些标题**看不出是同一件事**（聚类可能把两件事并到了一起），就写「杂项」，
               不要硬凑一个名字。
            4. 名字里**不要**出现引号、书名号、括号、编号。
            5. 只输出 JSON，不要解释、不要加代码块标记。

            ⚠️ 下面格式里的尖括号是**占位符**，不要把括号里的字照抄进答案。
            输出格式：{"label":"<名字>"}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"label":{"type":"string"}},"required":["label"]}""";

    private final FeedIndexMapper idx;
    private final ProviderRegistry providers;
    private final GpuGate gpuGate;
    private final RagProperties props;
    private final ObjectMapper mapper = new ObjectMapper();

    /** 一次命名几条。**显式上限**：一条 = 一次模型调用，攒批是为了少进几次模型。 */
    public record Result(int pending, int labeled, List<String> labels, String note) {
    }

    public Result labelPending(int limit) {
        List<Map<String, Object>> rows = idx.unlabeledTopics(limit);
        if (rows.isEmpty()) {
            return new Result(0, 0, List.of(), "没有待命名的议题");
        }
        if (!gpuGate.awaitIdle()) {
            return new Result(rows.size(), 0, List.of(), "GPU 正忙（有用户请求在跑），这次不抢");
        }
        ProviderRegistry.Ref ref = providers.resolveChat(props.getAgent().getUtilityModel());
        List<String> done = new ArrayList<>();
        for (Map<String, Object> r : rows) {
            long id = ((Number) r.get("id")).longValue();
            String titles = FeedValues.str(r.get("titles"));
            if (titles.isBlank()) {
                continue;
            }
            try {
                String reply = providers.chatJson(ref,
                        List.of(ChatMessage.system(PROMPT), ChatMessage.user("标题：\n" + titles)),
                        0.2, SCHEMA).content();
                JsonNode node = JsonExtract.parseObject(mapper, reply);
                String label = node == null ? "" : JsonExtract.string(node, "label", "");
                label = label.strip();
                if (!usable(label)) {
                    log.warn("议题 {} 的名字不可用，跳过：{}", id, label);
                    continue;
                }
                if (label.length() > 16) {
                    label = label.substring(0, 16);
                }
                idx.setTopicLabel(id, label);
                done.add(label);
            } catch (Exception e) {
                log.warn("议题 {} 命名失败：{}", id, e.getMessage());
            }
        }
        log.info("议题命名：待 {} 条 → 命名 {} 条（{}）", rows.size(), done.size(),
                done.isEmpty() ? "-" : String.join("、", done.subList(0, Math.min(8, done.size()))));
        return new Result(rows.size(), done.size(), done, null);
    }

    /** 格式示例里出现过的词 —— **模型会照抄示例值**（本项目记过两次），所以显式拒掉。 */
    private static final java.util.Set<String> PLACEHOLDERS =
            java.util.Set.of("事件名", "名字", "label", "...", "…");

    /** 名字得**有实义字符**（汉字/字母/数字），且不能是纯标点 —— 见类注释里那次「...」的教训。 */
    private static boolean usable(String s) {
        if (s == null || s.isBlank() || s.length() > 24) {
            return false;
        }
        if (PLACEHOLDERS.contains(s.strip()) || s.indexOf('<') >= 0 || s.indexOf('>') >= 0) {
            return false;   // 照抄了格式示例 / 占位符
        }
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (Character.isLetterOrDigit(c)) {
                return true;
            }
        }
        return false;
    }
}
