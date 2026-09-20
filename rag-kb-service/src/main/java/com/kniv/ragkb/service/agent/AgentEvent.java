package com.kniv.ragkb.service.agent;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Agent 执行过程中的一个事件，用于流式推给前端。
 *
 * <p>为什么每一步都要推：agent 模式一轮本地模型就要几十秒。用户看不到任何动静的话，
 * 体验比经典 RAG 还差 —— 明明做了更多事，却显得更卡。把「正在规划 / 检索到什么 /
 * 资料够不够 / 重新再找」实时推出去，等待才有意义。
 */
public record AgentEvent(String type, Map<String, Object> data) {

    public static final String PLAN = "plan";
    public static final String RETRIEVE = "retrieve";
    public static final String ASSESS = "assess";
    public static final String ANSWER = "answer";
    /**
     * 模型的思考片段。与 {@link #ANSWER} 严格分开 —— 前端应当折叠展示，
     * 绝不能混进正文：它是过程不是结论，混进去会污染回答。
     */
    public static final String THINKING = "thinking";
    public static final String META = "meta";
    /**
     * **这一轮的计时**（prefill / decode 分开）。
     *
     * <p>为什么单开一条而不是塞进 {@code done}：{@code done} 的载荷要穿过
     * ChatService.Outcome 四五个文件才到得了控制器；而这条只从生成处发出，
     * **客户端不认识它就忽略**，不动任何既有契约。
     */
    public static final String STATS = "stats";
    public static final String DONE = "done";
    public static final String ERROR = "error";

    public static AgentEvent of(String type, Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return new AgentEvent(type, m);
    }

    public static AgentEvent plan(int round, java.util.List<String> queries) {
        return of(PLAN, "round", round, "queries", queries);
    }

    public static AgentEvent retrieve(int round, String query, int hitCount, java.util.List<String> sources) {
        return of(RETRIEVE, "round", round, "query", query, "hits", hitCount, "sources", sources);
    }

    public static AgentEvent assess(int round, boolean enough, String reason, String missing) {
        return of(ASSESS, "round", round, "enough", enough, "reason", reason, "missing", missing);
    }

    public static AgentEvent answerToken(String text) {
        return of(ANSWER, "t", text);
    }

    /** @param text 一批思考片段（调用方已按块合并，不是逐 token） */
    public static AgentEvent thinking(String text) {
        return of(THINKING, "t", text);
    }
}
