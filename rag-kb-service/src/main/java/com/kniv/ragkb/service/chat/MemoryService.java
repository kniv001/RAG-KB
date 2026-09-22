package com.kniv.ragkb.service.chat;

import com.kniv.ragkb.dao.mapper.MemoryItemMapper;
import com.kniv.ragkb.domain.entity.MemoryItem;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * **长期记忆**：跨会话存活的那类事实。见 {@link MemoryItem} 的类注释。
 *
 * <p><b>合并是纯机械的，不请模型</b> —— 这一条是刻意的，也是这个类最大的设计选择。
 *
 * <p>会话摘要那边的合并要模型，因为它面对的是**原始对话**（一大段自然语言，
 * 要判断"哪句是持久事实"）。而这里面对的是**已经提纯过的一行一条**
 * （`主题：曾经 → 现在`，由摘要服务产出）—— 判断只剩两件，而且都 mechanical：
 * <ol>
 *   <li>这是不是同一个主题？→ 比主题串（{@link #sameTopic}）</li>
 *   <li>值变了吗？→ 比右端（不等就补覆盖边，`旧 → 新`）</li>
 * </ol>
 * 两件都不需要语义理解，于是**不需要模型**。好处直接：确定性、可复现、
 * 不花时间、也不会引入模型那类"把旧列表原样抄回来"的退化解。
 *
 * <p>这与项目里反复出现的那条一致：**判据算得出来的事，先想能不能用代码补**
 * （覆盖边 / 数字判据 / 只增不删，都是这么定下来的）。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class MemoryService {

    private final MemoryItemMapper memory;

    /** 注入提示词时的条数上限 —— 超了截尾（{@link #listAll} 按最近更新排前面）。 */
    public static final int MAX_ITEMS = 30;

    /** 「曾经」那一侧的空写法，与摘要服务同一套。 */
    private static final Set<String> NO_HISTORY = Set.of("—", "-", "", "无", "？", "?");

    /**
     * 把一批**已经提纯过的**记忆行并进长期记忆。
     *
     * @param lines  形如 `主题：曾经 → 现在` 的行；解析不出这个形状的**原样放过**（不猜）
     * @param convId 来源会话，只在**首次**写下某个主题时记录
     */
    public void absorb(List<String> lines, String convId) {
        if (lines == null || lines.isEmpty()) {
            return;
        }
        Map<String, MemoryItem> byTopic = new LinkedHashMap<>();
        for (MemoryItem m : memory.listAll()) {
            byTopic.put(m.getTopic(), m);
        }
        int added = 0, changed = 0;
        for (String raw : lines) {
            String[] p = splitItem(raw);
            if (p == null) {
                continue;                       // 不是 `主题：值` 的形状，不猜
            }
            String topic = p[0], now = p[2];
            MemoryItem hit = find(byTopic, topic);
            if (hit == null) {
                MemoryItem m = new MemoryItem();
                m.setTopic(topic);
                m.setItem(topic + "：" + now);
                m.setSrcConv(convId);
                memory.insert(m);
                byTopic.put(topic, m);
                added++;
                continue;
            }
            if (now.equals(currentValue(hit.getItem()))) {
                continue;                       // 值没变 —— **不写库**，否则 updated_at 会一直被顶新
            }
            // **补覆盖边**：上一条的右值就是这一条的左值。与会话摘要同一手法。
            String merged = topic + "：" + currentValue(hit.getItem()) + " → " + now;
            memory.overwrite(topic, merged);
            hit.setItem(merged);
            changed++;
        }
        if (added + changed > 0) {
            log.info("长期记忆：新增 {} 条、覆盖 {} 条（来源会话 {}）", added, changed, convId);
        }
    }

    /** 取全部记忆行（已按最近更新排序）。 */
    public List<String> list() {
        List<String> out = new ArrayList<>();
        for (MemoryItem m : memory.listAll()) {
            out.add(m.getItem());
        }
        return out;
    }

    /** 渲染成提示词里的一段；空则返回 null（调用方据此不加这一段）。 */
    public String render() {
        List<String> items = list();
        if (items.isEmpty()) {
            return null;
        }
        List<String> kept = items.size() > MAX_ITEMS ? items.subList(0, MAX_ITEMS) : items;
        StringBuilder sb = new StringBuilder();
        sb.append("【长期记忆】（**跨会话**攒下来的事实与约定，一行一条。")
                .append("它与本次对话无关，是过去定下来的 —— 与本次对话冲突时**以本次对话为准**）\n");
        for (String it : kept) {
            sb.append("- ").append(it).append('\n');
        }
        if (items.size() > kept.size()) {
            sb.append("（共 ").append(items.size()).append(" 条，这里只列最近 ")
                    .append(kept.size()).append(" 条）\n");
        }
        return sb.toString();
    }

    /** 同主题判定：先精确、再走**最长公共子串**（与会话摘要的 `hasCounterpart` 同一套）。 */
    private MemoryItem find(Map<String, MemoryItem> byTopic, String topic) {
        MemoryItem exact = byTopic.get(topic);
        if (exact != null) {
            return exact;
        }
        for (Map.Entry<String, MemoryItem> e : byTopic.entrySet()) {
            if (sameTopic(e.getKey(), topic)) {
                return e.getValue();
            }
        }
        return null;
    }

    /**
     * 两个主题串算不算同一个。
     *
     * <p>判据用**归一化后的互相包含 / 最长公共子串 ≥ 4** —— 比会话摘要那条（≥6）**松**。
     * 为什么松：主题串本来就短（「分块粒度」四个字），阈值 6 会把绝大多数主题判成不同；
     * 而这里判错的代价不对称 —— 判成不同只是多一行（冗余），判成相同才会**覆盖掉一条真事实**。
     * 松一点是往"多留"那边倒。
     */
    private static boolean sameTopic(String a, String b) {
        String x = normalise(a), y = normalise(b);
        if (x.isEmpty() || y.isEmpty()) {
            return false;
        }
        if (x.contains(y) || y.contains(x)) {
            return true;
        }
        return longestCommon(x, y) >= 4;
    }

    /** `主题：曾经 → 现在` → [主题, 曾经, 现在]；不是这个形状返回 null。 */
    static String[] splitItem(String it) {
        int a = it.indexOf('：');
        int b = it.indexOf('→');
        if (a <= 0 || b <= a) {
            return null;
        }
        return new String[]{it.substring(0, a).strip(),
                it.substring(a + 1, b).strip(), it.substring(b + 1).strip()};
    }

    /** 一行记忆的**当前值** = 右端。 */
    static String currentValue(String item) {
        String[] p = splitItem(item);
        return p == null ? item : p[2];
    }

    private static String normalise(String s) {
        return s == null ? "" : s.replace("→", " ").replace("—", " ").replaceAll("\\s+", "").strip();
    }

    private static int longestCommon(String x, String y) {
        int best = 0;
        int[] prev = new int[y.length() + 1];
        for (int i = 1; i <= x.length(); i++) {
            int[] cur = new int[y.length() + 1];
            for (int j = 1; j <= y.length(); j++) {
                if (x.charAt(i - 1) == y.charAt(j - 1)) {
                    cur[j] = prev[j - 1] + 1;
                    best = Math.max(best, cur[j]);
                }
            }
            prev = cur;
        }
        return best;
    }
}
