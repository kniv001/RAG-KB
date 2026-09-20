package com.kniv.ragkb.provider;

/**
 * **一次 LLM 调用的计时**（Ollama 在响应末尾本来就给了，此前被整个丢掉）。
 *
 * <p>为什么值得接住：客户端只能量到「发出 → 首字」这段总时长，而它里面**混着两种
 * 完全不同性质的开销** ——
 * <ul>
 *   <li>{@link #promptMs}（prefill）：**并行**预处理整个提示词，**随装入的 token 数增长**
 *       ⇒ 这是「上下文长度 ↔ 首字延迟」那条耦合的**直接度量**；</li>
 *   <li>{@link #evalMs}（decode）：**逐 token 串行**生成，除以 {@link #evalTokens} 就是
 *       真实的生成速率 —— 它决定"回答写多长要等多久"，与提示词长短基本无关。</li>
 * </ul>
 * 分开之后，"提速"才有方向：prefill 大 ⇒ 该少装；decode 是瓶颈 ⇒ 只能少写。
 *
 * <p>{@link #promptTokens} 还顺手解决另一件事：**真正装进去多少 token** 以前只能靠估算
 * （2026-09-20 估的是 9.3K），现在有实测值。
 *
 * @param loadMs       模型加载（换模型后第一次会很大，热了接近 0）
 * @param promptTokens 提示词 token 数（prefill 的输入规模）
 * @param promptMs     prefill 耗时
 * @param evalTokens   生成的 token 数
 * @param evalMs       生成耗时
 * @param totalMs      模型侧总耗时
 */
public record ChatStats(long loadMs, long promptTokens, long promptMs,
                        long evalTokens, long evalMs, long totalMs) {

    /** 生成速率（token/秒）；没有生成就返回 0。 */
    public double tokensPerSecond() {
        return evalMs > 0 ? evalTokens * 1000.0 / evalMs : 0;
    }

    public static ChatStats none() {
        return new ChatStats(0, 0, 0, 0, 0, 0);
    }
}
