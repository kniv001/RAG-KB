package com.kniv.ragkb.service.chat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.kniv.ragkb.dao.mapper.ConversationMapper;
import com.kniv.ragkb.dao.mapper.MessageMapper;
import com.kniv.ragkb.domain.entity.Conversation;
import com.kniv.ragkb.domain.entity.Message;
import com.kniv.ragkb.provider.ProviderRegistry;
import com.kniv.ragkb.provider.model.ChatMessage;
import com.kniv.ragkb.service.agent.JsonExtract;
import com.kniv.ragkb.service.config.RagProperties;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 会话滚动摘要 —— 历史索引漏召时的兜底。
 *
 * <p><b>为什么需要</b>：向量检索只给 top-k，召不回就丢了，而且<b>没有第二次机会</b>。
 * 指代与省略尤其致命 —— 用户问「那它呢」，检索很可能匹配不上定义「它」的那一轮，
 * 因为那轮的文本里根本没有「它」这个词。摘要覆盖全部历史，粗糙但不会完全丢。
 *
 * <p><b>成本怎么压下来的</b>：摘要本该是一次完整的生成调用（在这个模型上约 20 秒），
 * 那样后台跑也会和新请求抢 GPU。这里走结构化输出（关思考 + 语法约束），
 * 实测同类调用 0.3~1.6 秒 —— 摘要不需要深度推理，它是归纳不是解题。
 *
 * <p><b>为什么是增量的</b>：每次都重读整个会话重算的话，成本随轮数线性增长。
 * 这里只把「新掉出最近窗口」的那几条并入已有摘要，锚点是
 * {@code conversations.summary_upto}。
 *
 * <p><b>为什么异步</b>：它只是锦上添花，不该让用户多等一秒。
 * 用显式线程池而不是 {@code @Async} —— 后者靠代理生效，同类内部调用会静默失效。
 * 单线程是为了最多只有一个摘要在算，避免长会话里堆积。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class SummaryService {

    /**
     * 摘要的**形状**是实测定的：一行一条，不是一段连贯文字。
     *
     * <p>2026-09-18 的「记忆当检索」实验（拿决策台账当记忆库、11 问 × 5 组）量到：
     * **一行一条的索引本身就承载了大半** —— 只给 22 行索引（2019 token，全量方案的 11%）
     * 就答对了 10/11，而召回正文只把关键短语命中从 19/39 抬到 25/39。原因很直接：
     * 那一行写的是**结论**，不是"更新了某个文件"。
     *
     * <p>所以这里把摘要从段落改成条目。**只改形状，不改语义** ——
     * 合并、增量、异步、上限都没动，方便出问题时判断是哪一处引起的。
     *
     * <p>条目再进一步写成**变化式**（「曾经 → 现在」），依据是同一晚的三次重复对照
     * （`tools/summary-shape-probe.py`，用例：旧摘要里有"块大小 600"和一条用户偏好，
     * 新对话把块大小改成 450）：
     * <pre>
     *   写法      有箭头   保住旧值   保住"没变化"的偏好
     *   状态式      0/3      0/3        1/3   ← 三次里丢两次
     *   变化式      3/3      3/3        3/3
     * </pre>
     * 关键不在箭头本身，而在**形状是个完整性约束**：强制"两边都要写"，就把"没变化"
     * 也逼成显式的 {@code — → 现状}；状态式的自由形式会让模型把不变的事实静默丢掉 ——
     * 而那恰恰是"用户要求记住、丢了就找不回来"的那一类。
     *
     * <p><b>已知失效模式</b>（用例C 长杂输入，n=1）：把两条并列事实压进一个箭头会走形，
     * 例如把"新文档保留 Markdown / 旧文档仍是一行标题"写成
     * 「旧文档结构：Markdown 保留 → 一行标题」。看到这类条目要当噪声处理。
     */
    private static final String PROMPT = """
            你是对话记忆的整理器。把「新增对话」并入「已有记忆」，输出**一行一条**的条目。

            **每条写「曾经 → 现在」**：箭头左边是之前的值，右边是**现在**的值。
            读的人按位置判断当前状态，不必回头比较两条。

            要求：
            1. **新增对话是新信息的唯一来源，每条结论都必须落到条目上** —— 已有条目为空时同样如此。
               任何情况下都不许返回空数组。
            2. 一条一个事实或结论，不要写成段落，不要写「用户问了…助手回答了…」的流水账。
            3. 保留具体信息：讨论的主题、得出的结论、用户明确表达过的偏好或约束、尚未解决的问题。
               用户明确要求记住的任何内容必须原样保留 —— 名字、代号、数字、约定、日期。
               这类信息一旦丢掉就再也找不回来，比主题概括重要得多。
            4. **本次没被改变的事实照样要写**，左边写「—」（如「偏好：— → 表格而非选项式」）。
               会话记忆里大部分本来就没变 —— 只写变化会把这些整段丢掉。
            5. 同一主题只留一条。**本次被改变时，把上一个值写到左边**（不是写「—」），
               右边是现在的值。不要只写新值，不要层层叠加，也不要直接删掉
               —— 为什么变本身是有用的。
            6. **代价、限制、反面结论要单独写出来**，不要跟好处挤在同一行。
            7. 输出不超过 20 条，每条不超过 40 字。

            只输出 JSON：{"items":["主题：曾经 → 现在"]}""";

    private static final String SCHEMA = """
            {"type":"object","properties":{"items":{"type":"array","items":{"type":"string"}}},"required":["items"]}""";

    /** 条目硬上限：比提示词里的 12 宽松一档，只用来挡住模型偶发的刷屏 */
    private static final int MAX_ITEMS = 20;

    /**
     * 一次合并最多试几次。
     *
     * <p>2026-09-18 实测：**合并是双模态的** —— 同一份输入连发五次，原有 8 条事实的存活数是
     * {@code [7, 7, 2, 8, 2]}，也就是**五次里有两次一次丢掉 6 条**。塌陷模式高度一致：
     * 丢的全是 {@code — → 现状} 那批（没变化的事实），正是提示词第 4 条要保的。
     *
     * <p>所以这里用最省事的机械判据兜底：**合并后条目数不该比原来少**（健康的合并是
     * 8→9/10，塌陷是 8→4/5）。少了就重试，取条目最多的那次 —— 单次成功率约六成，
     * 三次就是 1−0.4³ ≈ 94%。健康时只发一次，不浪费。
     */
    private static final int MERGE_ATTEMPTS = 3;

    /** 「数字 + 冒号且后面不是数字」——可疑形态（时间 12:30 这类后面是数字，不会命中） */
    private static final java.util.regex.Pattern DIGIT_COLON =
            java.util.regex.Pattern.compile("(\\d{2,})\\s*[:：](?!\\d)");
    private static final java.util.regex.Pattern ANY_NUMBER =
            java.util.regex.Pattern.compile("\\d+");

    /**
     * **第二种糊法：「首位之后插冒号」**（{@code 24576 → 2:4576}）。
     *
     * <p>上面那条**两条排除条件同时把它挡在外面**：冒号前只有 1 位（{@code \d{2,}} 不匹配）、
     * 冒号后正是数字（{@code (?!\d)} 也不匹配）。所以另起一条，**故意写得很窄**：
     * <ul>
     *   <li>冒号前**恰好 1 位** —— 时间的小时是 2 位（{@code 12:30}），天然排除</li>
     *   <li>冒号后**≥4 位** —— 时间的分钟是 2 位（{@code 2:30}），天然排除</li>
     *   <li>而且**拼起来必须真在输入里出现** —— 否则不动</li>
     * </ul>
     * 三条合起来，能误伤的只剩「1 位数 : 4 位数」这种罕见写法。
     * 规则已在 {@code tools/digit-repair-rule-probe.py} 上验过 18/18（含 4 条误伤防线）。
     */
    private static final java.util.regex.Pattern DIGIT_INNER_COLON =
            java.util.regex.Pattern.compile("(?<![\\d:])(\\d)\\s*[:：](\\d{4,})(?![\\d])");

    private final MessageMapper messages;
    private final ConversationMapper conversations;
    private final ProviderRegistry providers;
    private final RagProperties props;
    private final ObjectMapper mapper;
    private final com.kniv.ragkb.service.config.GpuGate gpuGate;

    private final ExecutorService pool = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "conv-summary");
        t.setDaemon(true);
        return t;
    });

    /** 同一会话同一时刻只算一次，避免连续追问时堆积 */
    private final Set<String> inFlight = ConcurrentHashMap.newKeySet();

    /** 读摘要给提示词用。没有就返回 null。 */
    public String summaryOf(String convId) {
        if (convId == null || convId.isBlank()) {
            return null;
        }
        Conversation c = conversations.selectById(convId);
        String s = c == null ? null : c.getSummary();
        return (s == null || s.isBlank()) ? null : s;
    }

    /**
     * 触发一次异步增量更新。不满足条件就什么都不做 —— 这个方法会在每轮问答后调用，
     * 必须廉价。
     */
    public void maybeUpdate(String convId) {
        RagProperties.Summary cfg = props.getSummary();
        if (!cfg.isEnabled() || convId == null || convId.isBlank()) {
            return;
        }
        try {
            // 「最近窗口之外最新的那一条」—— 窗口内的原文还要给模型看，
            // 提前摘进摘要里是重复的
            Long upto = messages.idAtOffset(convId, cfg.getWindowMessages());
            if (upto == null) {
                return;   // 消息还没多到溢出窗口
            }
            Conversation conv = conversations.selectById(convId);
            if (conv == null) {
                return;
            }
            long from = conv.getSummaryUpto() == null ? 0L : conv.getSummaryUpto();
            if (upto <= from) {
                return;   // 没有新东西掉出窗口
            }
            List<Message> fresh = messages.listBetween(convId, from, upto, cfg.getBatchMessages());
            if (fresh.size() < cfg.getBatchMessages()) {
                return;   // 攒够一批再算，避免每轮都调一次模型
            }
            if (!inFlight.add(convId)) {
                return;   // 上一次还没算完
            }
            String old = conv.getSummary();
            long newUpto = fresh.get(fresh.size() - 1).getId();
            pool.submit(() -> {
                try {
                    // 等用户静默再开始。本机只有一个推理槽（OLLAMA_NUM_PARALLEL=1），
                    // 实测后台任务跑 3091ms 时用户请求要等 3095ms —— 等满。
                    // 摘要晚做几分钟没有代价，让用户等有代价。
                    if (!gpuGate.awaitIdle()) {
                        return;
                    }
                    String merged = summarize(old, fresh);
                    if (merged != null && !merged.isBlank()) {
                        conversations.updateSummary(convId, merged, newUpto);
                        log.debug("会话 {} 摘要已更新到消息 {}（{} 字）", convId, newUpto, merged.length());
                    }
                } catch (Exception e) {
                    log.warn("摘要更新失败（不影响问答）：{}", e.getMessage());
                } finally {
                    inFlight.remove(convId);
                }
            });
        } catch (Exception e) {
            log.warn("摘要调度失败（不影响问答）：{}", e.getMessage());
        }
    }

    private String summarize(String old, List<Message> fresh) throws Exception {
        StringBuilder user = new StringBuilder();
        user.append("已有条目：\n").append(old == null || old.isBlank() ? "（无，这是第一次）" : old)
                .append("\n\n新增对话：\n");
        for (Message m : fresh) {
            String text = m.getContent() == null ? "" : m.getContent().replace('\n', ' ').strip();
            if (text.length() > 400) {
                text = text.substring(0, 400) + "…";
            }
            user.append("assistant".equals(m.getRole()) ? "助手：" : "用户：").append(text).append('\n');
        }

        ProviderRegistry.Ref ref = providers.resolveChat(props.getAgent().getUtilityModel());
        List<ChatMessage> msgs =
                List.of(ChatMessage.system(PROMPT), ChatMessage.user(user.toString()));
        int oldCount = old == null ? 0 : (int) old.lines().filter(l -> !l.isBlank()).count();

        List<String> oldItems = old == null ? List.of()
                : old.lines().map(String::strip).filter(l -> !l.isBlank()).toList();

        List<String> best = new ArrayList<>();
        String lastReply = "";
        for (int attempt = 1; attempt <= MERGE_ATTEMPTS; attempt++) {
            lastReply = providers.chatJson(ref, msgs, 0.2, SCHEMA).content();
            List<String> items = parseItems(lastReply);
            if (better(items, best, oldItems)) {
                best = items;
            }
            // 两条都满足才算健康合并：条目不比原来少（防塌陷）**且**旧条目都有着落（防零星丢失）
            if (best.size() >= oldCount && unrepresented(oldItems, best) == 0) {
                break;
            }
            log.debug("合并后条目 {}（原有 {}）、旧条目没着落的 {} 条，第 {} 次重试",
                    best.size(), oldCount, unrepresented(oldItems, best), attempt);
        }

        // 空数组**不能**覆盖已有摘要 —— 语法约束下 {"items":[]} 是最省的合法输出，
        // 而它在语义上等于「把记忆清空」。宁可这次不更新（下一批再试）。
        if (best.isEmpty()) {
            log.debug("摘要输出为空数组或无法解析，本次跳过（保留原摘要）。片段：{}", clip(lastReply));
            return null;
        }
        if (oldCount > 0 && best.size() < oldCount) {
            log.warn("摘要合并三次都少于原有条目（{} → {}）—— 可能有事实被丢掉，本次照用但要留意",
                    oldCount, best.size());
        }

        // 覆盖边：**机械补**，不问模型。
        //
        // 依据 2026-09-20 的实测（tools/supersede-probe.py）：
        //   给「旧条目 K：— → 600」+「新增对话把 600 改成 450」，
        //   模型输出 `K：— → 450` —— **旧值一律被丢掉**，12/12，覆盖边根本没形成。
        //   提示词里明写「本次被改变时，把上一个值写到左边」之后**照旧 12/12 丢旧**
        //   （那一改只把「没变也要写」的对照组从 8/12 抬到 12/12）。
        // ⇒ 模型做不了这个操作，但它是**纯字符串操作**：上一条的右值就是这一条的左值。
        //   补完之后 12/12 全落进「✅覆盖」。这与 missingOld（只增不删）是同一类补丁。
        //
        // 放在 missingOld **之前**：补出的 `K：600 → 450` 含旧值，于是它会被认作
        // 「旧条目已着落」，不会再把 `K：— → 600` 原样补进来造成两条并存。
        best = addCoverageEdges(oldItems, best);

        // 只增不删：三次重试都没着落的旧条目**原样补回**。
        //
        // 依据 2026-09-19 的四臂对照（tools/summary-loss-probe.py，各 2 链 × 5 轮增量合并）：
        // 现状 10 轮里 4 轮塌到 1~4 条（丢的全是「没变化的事实」），**且塌了就回不来**
        // ——下一轮从短列表继续；补回后每轮 8/8。代价是列表变长（7.5 → 10.5 条），陈旧行实测 0。
        // 顺带否掉一个猜想：重试各带唯一编号（破 KV 前缀复用）**无效且更差**（最惨一轮剩 1 条），
        // 塌陷是模型本身的双模态，不是缓存的产物。
        List<String> missing = missingOld(oldItems, best);
        for (String o : missing) {
            if (best.size() >= MAX_ITEMS) {
                break;
            }
            best.add(o);
            log.warn("合并丢了旧条目，原样补回（只增不删）：{}", o);
        }

        warnIfNumberCorrupted(best, (old == null ? "" : old) + "\n" + user);

        // **冻结检测**：输出与输入**逐字相同** ⇒ 这一轮新信息一条都没落地。
        //
        // 为什么需要它：现有的告警只覆盖"条目变少"（丢事实）那一个方向。
        // 而 2026-09-20 实测出**反方向**的退化 —— 列表长到 18 条时模型倾向照抄，
        // 5 轮里有 2 轮「新信息落地 0/2」而条目数一条不差，所有已有判据都看不见。
        // 只增不删治的是丢，治不了这个。
        if (oldCount > 0 && best.size() == oldCount && best.equals(oldItems)) {
            log.warn("摘要合并输出与输入逐字相同 —— 本轮新信息可能一条都没落地（冻结），片段：{}",
                    clip(user.toString()));
        }
        return String.join("\n", best);
    }

    /**
     * 挑更好的那一版：**先看旧条目有没有着落，再看条目多少**。
     *
     * <p>为什么不能只看条目数：实测有一类塌陷是"总数不降但换了内容" ——
     * 8 条旧事实变成 9 条，可其中一条（用户偏好）被挤掉了。
     * 计数判据看不见这种丢失，所以要用「旧条目在新输出里找不找得到」来判。
     */
    private boolean better(List<String> cand, List<String> best, List<String> oldItems) {
        if (best.isEmpty()) {
            return !cand.isEmpty();
        }
        int a = unrepresented(oldItems, cand);
        int b = unrepresented(oldItems, best);
        return a != b ? a < b : cand.size() > best.size();
    }

    /** 旧条目里有多少条在新输出中找不到着落（改写过也算，见 {@link #hasCounterpart}）。 */
    private int unrepresented(List<String> oldItems, List<String> items) {
        return missingOld(oldItems, items).size();
    }

    /** 找不到着落的旧条目**有哪些** —— 用于只增不删时的原样补回。 */
    private List<String> missingOld(List<String> oldItems, List<String> items) {
        List<String> missing = new ArrayList<>();
        for (String o : oldItems) {
            boolean found = false;
            for (String it : items) {
                if (hasCounterpart(o, it)) {
                    found = true;
                    break;
                }
            }
            if (!found) {
                missing.add(o);
            }
        }
        return missing;
    }

    /**
     * 两条是否算"同一条"：**归一化**之后最长公共子串 ≥ min(6, 旧条目长度的一半)。
     *
     * <p>先去掉变化式标记（{@code —} {@code →}）再比 —— 否则会误报：
     * 旧「分块粒度：— → 600 字」对新「分块粒度：600 字 → 450 字」，
     * 事实明明还在（箭头左边），可箭头一插进来公共子串就断在「分块粒度：」上（5 字 &lt; 6）。
     * 归一化后是「分块粒度：600 字」对「分块粒度：600 字 450 字」，一眼能看出来。
     *
     * <p>阈值 6：中文 40 字的条目里，6 字连续重合已经不像巧合。
     */
    private boolean hasCounterpart(String a, String b) {
        String x = normalise(a);
        String y = normalise(b);
        int need = Math.min(6, Math.max(2, x.length() / 2));
        int[] prev = new int[y.length() + 1];
        for (int i = 1; i <= x.length(); i++) {
            int[] cur = new int[y.length() + 1];
            for (int j = 1; j <= y.length(); j++) {
                if (x.charAt(i - 1) == y.charAt(j - 1)) {
                    cur[j] = prev[j - 1] + 1;
                    if (cur[j] >= need) {
                        return true;
                    }
                }
            }
            prev = cur;
        }
        return false;
    }

    /** 去掉变化式标记与多余空白，只留内容本身。 */
    private static String normalise(String s) {
        return s.replace("→", " ").replace("—", " ").replaceAll("\\s+", "").strip();
    }

    private List<String> parseItems(String reply) {
        JsonNode node = JsonExtract.parseObject(mapper, reply);
        List<String> items = new ArrayList<>();
        if (node != null && node.path("items").isArray()) {
            for (JsonNode it : node.path("items")) {
                String s = it.asText("").strip();
                if (!s.isEmpty()) {
                    items.add(s);
                }
                if (items.size() >= MAX_ITEMS) {
                    break;
                }
            }
        }
        return items;
    }

    /** 「曾经」那一侧的空写法 —— 见到这些就说明模型没写历史。 */
    private static final java.util.Set<String> NO_HISTORY =
            java.util.Set.of("—", "-", "", "无", "？", "?");

    /**
     * **覆盖边（机械补）**：同一主题、新条目左边是「—」而上一条有值时，把上一条的右值搬到左边。
     *
     * <p>形状要求是 `主题：曾经 → 现在`。解析不出这个形状的条目**原样放过**（不猜）。
     */
    private List<String> addCoverageEdges(List<String> oldItems, List<String> items) {
        java.util.Map<String, String> prev = new java.util.HashMap<>();
        for (String it : oldItems) {
            String[] p = splitItem(it);
            if (p != null) {
                prev.put(p[0], p[2]);
            }
        }
        List<String> out = new ArrayList<>(items.size());
        for (String it : items) {
            String[] p = splitItem(it);
            if (p != null && NO_HISTORY.contains(p[1])) {
                String was = prev.get(p[0]);
                if (was != null && !was.isBlank() && !was.equals(p[2])) {
                    log.debug("补覆盖边：{}（{} → {}）", p[0], was, p[2]);
                    it = p[0] + "：" + was + " → " + p[2];
                }
            }
            out.add(it);
        }
        return out;
    }

    /** `主题：曾经 → 现在` → [主题, 曾经, 现在]；不是这个形状返回 null。 */
    private static String[] splitItem(String it) {
        int a = it.indexOf('：');
        int b = it.indexOf('→');
        if (a <= 0 || b <= a) {
            return null;
        }
        return new String[]{it.substring(0, a).strip(),
                it.substring(a + 1, b).strip(), it.substring(b + 1).strip()};
    }

    /**
     * 数字保真检查：**只记日志，不改内容**。
     *
     * <p>背景（2026-09-18 实测）：qwen3:4b 在改写时会把某些数字的末位换成冒号 ——
     * {@code 600 → "60:"}（有时自己补回成 {@code "60: 600"}）、{@code 3000 → "300:"}、
     * {@code 24576 → "2457："}。特征是**值特异且确定性**：扫了 18 个数字只有 3 个中招，
     * 温度 0.0/0.2/0.7 一模一样，提示词里明确禁止也照样发生 —— 是 4B 解码层的毛病，
     * 不是提示词能修的。qwen3:8b 同条件下干净（但换模型会让两个模型来回换载，代价更大）。
     *
     * <p>判据用机械的一条：**摘要里的数字必须能在输入里找到**。找不到、或它的前缀
     * 在输入里对应一个更长的数字，就说明末位大概率被吃了 —— 记 warn 让人能搜到。
     */
    private void warnIfNumberCorrupted(List<String> items, String source) {
        java.util.Set<String> srcNums = new java.util.HashSet<>();
        java.util.regex.Matcher am = ANY_NUMBER.matcher(source);
        while (am.find()) {
            srcNums.add(am.group());
        }
        for (String it : items) {
            java.util.regex.Matcher m = DIGIT_COLON.matcher(it);
            while (m.find()) {
                String digits = m.group(1);
                if (srcNums.contains(digits)) {
                    continue;                       // 输入里本来就有「300:」这种写法，是正常的
                }
                boolean prefixOfLonger = false;
                for (String s : srcNums) {
                    if (s.length() > digits.length() && s.startsWith(digits)) {
                        prefixOfLonger = true;
                        break;
                    }
                }
                if (prefixOfLonger) {
                    log.warn("摘要里的数字可能被吃掉了末位：「{}」—— 输入里有更长的同前缀数字。条目：{}",
                            digits + ":", it);
                }
            }
            // **影子模式：把「会修成什么」也记下来**（仍然不改内容）。
            //
            // 为什么先记不修：规则本身已经补齐并验过误伤（`tools/digit-repair-rule-probe.py`
            // 18/18 通过，含 4 条误伤防线：`12:30` / `2:30` / 不在输入里的拼接 / 版本号）。
            // **但生产上还没机会响过** —— 日志覆盖的 2.5 小时里跑的全是单轮问答，
            // 摘要那条路根本没跑过。**为一场没机会发生的病开药，风险是无法评估的。**
            // 记下影子输出，等真出现多轮会话就有真实案例可判。
            java.util.regex.Matcher ic = DIGIT_INNER_COLON.matcher(it);
            while (ic.find()) {
                String joined = ic.group(1) + ic.group(2);
                if (srcNums.contains(joined)) {
                    log.warn("【影子·若启用会改成】「{}」→「{}」　条目：{}", ic.group(0), joined, it);
                }
            }
        }
    }

    private static String clip(String s) {
        if (s == null) {
            return "";
        }
        String t = s.replace('\n', ' ').strip();
        return t.length() <= 160 ? t : t.substring(0, 160) + "…";
    }
}
