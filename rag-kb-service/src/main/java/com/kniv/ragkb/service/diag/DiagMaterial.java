package com.kniv.ragkb.service.diag;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * **把"这一题最终注入了什么"留在内存里，供只读诊断端点取**（2026-09-29 加）。
 *
 * <h2>为什么要有它</h2>
 *
 * 量材料的那条路（`tools/eval/material-probe.mjs` + `bench-material-probe.py`）此前只能
 * **发一个请求、睡够 9 秒、再去 grep 日志**里的
 * {@code 材料地板 0.65（<题目前 20 字>）：过滤后剩 N 句 / M 块}。那条路有三个脆弱点，
 * 每个都在这个项目里真实发生过：
 * <ol>
 *   <li><b>睡短了就静默漏题</b> —— 那条链实测 3~6 秒，给到 9 秒是留余量；
 *       一旦某题慢过 9 秒，读数就是"没采到"，而它**长得像"这题没问题"**；</li>
 *   <li><b>日志会轮转</b>（10MB）—— 证据被搬走，读到的只有后半段；</li>
 *   <li><b>按"问题前 20 字"当键</b>去日志里找 —— 键撞了/编码变了就找不到，
 *       而失败的样子同样是"没采到"。</li>
 * </ol>
 *
 * <p>而那几个数**服务端本来就算出来了**（它们就是那几行日志的内容）。
 * 所以这里只做一件事：**把它们顺手记下来，再开一个只读的口子**，
 * 让尺子拿到的是**返回值**而不是**日志文本**。
 *
 * <h2>边界（写清楚，免得被当成"又一个存储"）</h2>
 * <ul>
 *   <li>**只读、只在内存、只在最近 64 题** —— 重启即空，不落库、不进备份；
 *       它**不是**一个数据表，别拿它当记录使（要留证据仍然落盘到 `_runs/`）。</li>
 *   <li>**不改变任何行为**：每个方法都只是把已经算出来的数抄一份。
 *       这里出问题最多是诊断少了，不会影响回答 —— 所以全程不抛异常。</li>
 *   <li>**键与日志同一个**（问题前 20 字）—— 这样新旧两种读法**能对得上**，
 *       换尺子的时候可以先比一遍（本项目的老规矩：先证"能对上"，再谈数字）。</li>
 * </ul>
 */
@Slf4j
@Component
public class DiagMaterial {

    /** 只留最近这么多题。**故意小**：这是诊断，不是存储；也少留一点用户问题在内存里。 */
    public static final int CAP = 64;

    /** 与日志行 {@code 材料地板 …（<问题前 20 字>）} **完全同一个键** —— 两套读法要能对上。 */
    public static String key(String question) {
        if (question == null) {
            return "";
        }
        return question.length() > 20 ? question.substring(0, 20) : question;
    }

    /** 一题的读数。字段可空 = 那一步还没跑到（或那条路没走）。 */
    public static final class Snap {
        public String key;
        public String question;
        /** 最后一次更新的时刻（毫秒）—— 探针靠它判断"这次请求的读数到了没有"。 */
        public long at;
        /** 材料地板：过滤后**还剩几句 / 几块**（这就是那条日志的两个数）。 */
        public Integer aliveSent;
        public Integer aliveChunks;
        public Double floor;
        public String floorMode;
        /** 地板存活标定：各阈值下原有多少句（`代码判相关性` 那行）。 */
        public Integer band55;
        public Integer band60;
        public Integer band65;
        public Integer band70;
        /** 注入：几段 / 其中几段有句子 / 共几句（`句子注入` 那行）。 */
        public Integer injSegs;
        public Integer injCovered;
        public Integer injSent;
        /** 相邻块补全：命中几段 → 新增几段 → 候选几段。 */
        public Integer nbHit;
        public Integer nbAdded;
        public Integer nbCand;
        /** 本次生效的开关，省得读的人再去猜这一轮跑的是哪一臂。 */
        public Integer sentChunkK;
        public Integer neighbor;
        public Integer sentWindowM;

        Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("key", key);
            m.put("question", question);
            m.put("at", at);
            m.put("aliveSent", aliveSent);
            m.put("aliveChunks", aliveChunks);
            m.put("floor", floor);
            m.put("floorMode", floorMode);
            m.put("bands", band55 == null ? null
                    : Map.of("0.55", band55, "0.60", band60, "0.65", band65, "0.70", band70));
            m.put("injected", injSegs == null ? null
                    : Map.of("segs", injSegs, "covered", injCovered, "sent", injSent));
            m.put("neighbor", nbHit == null ? null
                    : Map.of("hit", nbHit, "added", nbAdded, "cand", nbCand));
            Map<String, Object> sw = new LinkedHashMap<>();
            sw.put("sentChunkK", sentChunkK);
            sw.put("neighbor", neighbor);
            sw.put("sentWindowM", sentWindowM);
            m.put("switches", sw);
            return m;
        }
    }

    /** **按插入序**的定长表：满了丢最旧的。所有访问都走这一个锁 —— 量级是"每秒几次"，不值得更复杂。 */
    private final Map<String, Snap> table = new LinkedHashMap<>();

    private synchronized Snap snap(String question) {
        String k = key(question);
        Snap s = table.get(k);
        if (s == null) {
            s = new Snap();
            s.key = k;
            s.question = question;
            table.put(k, s);
        }
        s.at = System.currentTimeMillis();
        // 满了丢最旧的（LinkedHashMap 的插入序 = 首次出现序，符合"最近 64 题"的直觉）
        while (table.size() > CAP) {
            String oldest = table.keySet().iterator().next();
            table.remove(oldest);
        }
        return s;
    }

    // ── 四个记录点：每一个都紧挨着对应的那行日志。**记的是同一批变量**，
    //    这样"从端点读"与"从日志读"在同一时刻必须给出同一个数（换尺子时要先比这个）。

    /**
     * **一次请求开始**：把这个键上的旧读数**清掉**。
     *
     * <p>⚠️ 不清会出静默的错：同一个键（问题前 20 字）上一轮留下的 {@code aliveSent} 还在，
     * 而这一轮的 {@code 相邻块补全} 一记就把 {@code at} 刷新到当前时刻 —— 探针读到的是
     * **上一轮的数字配上这一轮的时间戳**，看起来完全正常。**同名读数必须从空开始。**
     */
    public void begin(String question) {
        try {
            String k = key(question);
            Snap s = new Snap();
            s.key = k;
            s.question = question;
            s.at = System.currentTimeMillis();
            synchronized (this) {
                table.put(k, s);
                while (table.size() > CAP) {
                    table.remove(table.keySet().iterator().next());
                }
            }
        } catch (Exception e) {
            log.debug("诊断记录（开始）失败：{}", e.getMessage());
        }
    }

    /** 挨着 `材料地板 {}（{}）：过滤后剩 {} 句 / {} 块`。 */
    public void floor(String question, double fl, String mode, int aliveSent, int aliveChunks) {
        try {
            Snap s = snap(question);
            s.floor = fl;
            s.floorMode = mode;
            s.aliveSent = aliveSent;
            s.aliveChunks = aliveChunks;
        } catch (Exception e) {
            log.debug("诊断记录（地板）失败：{}", e.getMessage());
        }
    }

    /** 挨着 `代码判相关性[…]｜地板存活 …`。四个阈值下的存活数，用来标定地板该取多少。 */
    public void bands(String question, int b55, int b60, int b65, int b70) {
        try {
            Snap s = snap(question);
            s.band55 = b55;
            s.band60 = b60;
            s.band65 = b65;
            s.band70 = b70;
        } catch (Exception e) {
            log.debug("诊断记录（标定）失败：{}", e.getMessage());
        }
    }

    /** 挨着 `句子注入（{}）：注入 {} 段 / 其中 {} 段有句子 / 共 {} 句`。 */
    public void injected(String question, int segs, int covered, int sent) {
        try {
            Snap s = snap(question);
            s.injSegs = segs;
            s.injCovered = covered;
            s.injSent = sent;
        } catch (Exception e) {
            log.debug("诊断记录（注入）失败：{}", e.getMessage());
        }
    }

    /** 挨着 `相邻块补全（±{}）：命中 {} 段 → 新增 {} 段 → 候选 {} 段`。 */
    public void neighbor(String question, int hit, int added, int cand) {
        try {
            Snap s = snap(question);
            s.nbHit = hit;
            s.nbAdded = added;
            s.nbCand = cand;
        } catch (Exception e) {
            log.debug("诊断记录（补全）失败：{}", e.getMessage());
        }
    }

    /** 本轮的开关快照 —— 每个请求都记一次，免得读的人再去猜这一臂跑的是什么。 */
    public void switches(String question, int sentChunkK, int neighbor, int sentWindowM) {
        try {
            Snap s = snap(question);
            s.sentChunkK = sentChunkK;
            s.neighbor = neighbor;
            s.sentWindowM = sentWindowM;
        } catch (Exception e) {
            log.debug("诊断记录（开关）失败：{}", e.getMessage());
        }
    }

    /** 按问题取（键 = 前 20 字）。没有就返回 null —— 调用方要**明说"没有"**，不要静默补 0。 */
    public synchronized Map<String, Object> get(String question) {
        Snap s = table.get(key(question));
        return s == null ? null : s.toMap();
    }

    /** 最近的全部读数，**最新的在前**。 */
    public synchronized List<Map<String, Object>> all() {
        List<Map<String, Object>> out = new ArrayList<>(table.size());
        for (Snap s : table.values()) {
            out.add(s.toMap());
        }
        java.util.Collections.reverse(out);
        return out;
    }

    /** 最近一共记了几题（端点用它区分"没这题"与"一题都还没跑"）。 */
    public synchronized int size() {
        return table.size();
    }
}
