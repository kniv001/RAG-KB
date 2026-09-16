package com.kniv.ragkb.service.agent;

import java.util.ArrayList;
import java.util.List;

/**
 * 提示词预算：确保拼出来的提示词不会超出模型窗口。
 *
 * <p><b>为什么必须有这个</b>：实测（num_ctx=10240）提示词到 9920 token 时一切正常，
 * 再多一点就**不是渐进退化，是悬崖** —— Ollama 把整个上下文重置到 5122 token
 * （正好一半），而且**丢掉的是开头**，也就是系统提示词。
 *
 * <p>后果不是「回答变差」，是「回答不再受任何约束」：三段式规则、引用标注要求、
 * 「不得编造」全部消失，而模型**照常返回一个看起来正常的回答，不报任何错**。
 * 用户完全看不出来。实测送到 40000 字时模型连问题和结尾都没看到，仍然正常作答。
 *
 * <p><b>为什么在应用层做而不是调大 num_ctx</b>：显存不允许。8GB 卡上
 * num_ctx=10240 已经是 qwen3:4b + bge-m3 同时常驻的极限（见 docs/ollama-tuning.md）。
 *
 * <p>策略是按优先级裁：留下系统提示与问题，然后依次保主题概览、参考资料、
 * 最近历史、召回片段、摘要。裁的是「按顺序从后往前丢」而不是「均匀缩水」——
 * 缩水会让每一部分都变得残缺，而按优先级丢至少保证留下的那部分完整。
 */
public final class PromptBudget {

    private PromptBudget() {
    }

    /**
     * 中文的字符/token 比。实测 16000 字 ≈ 9859 token，即 1.62 字/token。
     *
     * <p>这里取 1.5 而不是 1.62：宁可**高估** token 数（提前裁剪），
     * 也不要低估（超限丢系统提示）。英文与代码的比值更高（约 4 字/token），
     * 用 1.5 估它们会明显高估 —— 方向是对的，保守总比冒险好。
     */
    private static final double CHARS_PER_TOKEN = 1.5;

    public static int estimateTokens(String text) {
        if (text == null || text.isEmpty()) {
            return 0;
        }
        return (int) Math.ceil(text.length() / CHARS_PER_TOKEN);
    }

    public static int estimateTokens(List<String> parts) {
        int n = 0;
        for (String p : parts) {
            n += estimateTokens(p);
        }
        return n;
    }

    /**
     * 按预算裁剪一批「可丢」的文本，返回保留下来的条数。
     *
     * <p>从**尾部**开始丢：调用方按重要性从高到低排列，尾部的先走。
     * 之所以不是均匀截断每一条，是因为半截的参考资料比没有更糟 ——
     * 它可能恰好把一个结论切成前后矛盾的半句。
     *
     * @param parts    从重要到不重要排列
     * @param budget   这些文本加起来允许占多少 token
     * @return 应当保留的前 N 条
     */
    public static int keepWithin(List<String> parts, int budget) {
        if (budget <= 0) {
            return 0;
        }
        int used = 0;
        for (int i = 0; i < parts.size(); i++) {
            int t = estimateTokens(parts.get(i));
            if (used + t > budget) {
                return i;
            }
            used += t;
        }
        return parts.size();
    }

    /**
     * 取**后** n 条。
     *
     * <p>为什么单独有一个：会话历史是**时间正序**（最旧在前）传给这里的，
     * 而「保留最近几轮」要的是末尾那几条。第一版直接用了 {@link #take}，
     * 于是保留了最旧的、丢掉了最新的 —— 正好和意图相反，
     * 而「那它呢」这类追问靠的恰恰是最近几轮。这种错不会有任何报错，
     * 只会让人觉得「模型怎么突然不记得刚才说的话了」。
     */
    public static <T> List<T> takeLast(List<T> list, int n) {
        if (list == null || list.isEmpty()) {
            return List.of();
        }
        if (n >= list.size()) {
            return list;
        }
        if (n <= 0) {
            return List.of();
        }
        return new ArrayList<>(list.subList(list.size() - n, list.size()));
    }

    /** 取前 n 条；n 会被夹到合法范围 */
    public static <T> List<T> take(List<T> list, int n) {
        if (list == null || list.isEmpty()) {
            return List.of();
        }
        if (n >= list.size()) {
            return list;
        }
        if (n <= 0) {
            return List.of();
        }
        return new ArrayList<>(list.subList(0, n));
    }
}
