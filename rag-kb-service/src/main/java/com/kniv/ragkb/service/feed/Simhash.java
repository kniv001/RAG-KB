package com.kniv.ragkb.service.feed;

/**
 * **近重复判定**：3-gram shingle → 64 位 simhash → LSH 分带。
 *
 * <p><b>为什么必须有它</b>：新闻的转载率极高（同一件事 N 家转述、只改标题与首段），
 * 不去重的话一次问答的 top-24 里可能有十几条是同一件事 ⇒ 预算被吃光、多样性塌缩，
 * 而"被挤掉的别的事件"事后救不回来。检索端的多样性重排只能在**已经召回的重复堆**里
 * 腾挪，救不回没进候选的那些 —— 所以去重要在**入库时**做。
 *
 * <p><b>为什么不是字符树/后缀树</b>：中文上真建后缀树有两个坑 —— 内存随语料线性涨、
 * 且要先切 shingle 才有意义。simhash 的形状是"**查几个桶**"而不是"跟全库比"：
 * 64 位切 4 段 16 位，任意一段相同即为候选，于是查重代价**不随库增长**。
 * 字符树留到第二层用（事件归并之后，在 N 条报道间找"哪句是转述、哪句是独家"）。
 *
 * <p><b>分带为什么不会漏</b>（鸽巢原理）：两个 simhash 汉明距离 ≤3 时，那 3 个不同的位
 * 最多落在 3 段里 ⇒ **必有一段完全相同** ⇒ 该段就是候选桶。所以"任意一段相同"这个
 * 候选条件是**完备的**，不是近似。反过来说，候选里距离 &gt;3 的要靠 {@link #hamming} 再筛一次。
 *
 * <p>⚠️ 归一化做得狠（去掉全部空白与标点、全角转半角、小写）—— 因为转载的差异几乎
 * 全在这些地方。但这也意味着**它判的是"文字重复"，不是"同一件事"**：同一事件的独家
 * 报道（用词完全不同）它判不出来，那是下一步"议题/主张"层的事。
 */
public final class Simhash {

    /** 分带数。4 段是 64 位 simhash 的常用取值，且刚好配"距离 ≤3 不漏"的鸽巢条件。 */
    public static final int BANDS = 4;

    /** 每段位数。4 × 16 = 64。 */
    public static final int BAND_BITS = 16;

    /** 判为近重复的汉明距离阈值。 */
    public static final int DUP_DISTANCE = 3;

    /** shingle 长度（字符 n-gram）。 */
    private static final int SHINGLE = 3;

    private static final long FNV_OFFSET = 0xcbf29ce484222325L;
    private static final long FNV_PRIME = 0x100000001b3L;

    private Simhash() {
    }

    /**
     * 归一化：**只留字母、数字与汉字**。
     *
     * <p>去掉空白与标点、全角转半角、转小写 —— 转载之间的差异几乎都在这些地方，
     * 而它们对"是不是同一篇"没有信息量。
     */
    public static String normalize(String text) {
        if (text == null || text.isEmpty()) {
            return "";
        }
        StringBuilder b = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            // 全角 ASCII（！～）转半角：U+FF01..U+FF5E 与 ASCII 差 0xFEE0
            if (c >= 0xFF01 && c <= 0xFF5E) {
                c = (char) (c - 0xFEE0);
            } else if (c == 0x3000) {
                c = ' ';
            }
            if (Character.isLetterOrDigit(c)) {
                b.append(Character.toLowerCase(c));
            }
        }
        return b.toString();
    }

    /**
     * 3-gram shingle 加权投票得到 64 位指纹。
     *
     * <p>权重取 shingle 的**出现次数**（重复出现的短语更能代表这篇），
     * 每一维按"该位是 1 则加权重、是 0 则减权重"累加，最后取符号。
     */
    public static long of(String text) {
        String s = normalize(text);
        if (s.isEmpty()) {
            return 0L;
        }
        if (s.length() < SHINGLE) {
            // 短到切不出 3-gram：退化成"整串一个 shingle"，仍返回确定值（不是 0）
            return hash64(s);
        }
        java.util.Map<Long, Integer> freq = new java.util.HashMap<>();
        for (int i = 0; i + SHINGLE <= s.length(); i++) {
            freq.merge(hash64(s.substring(i, i + SHINGLE)), 1, Integer::sum);
        }
        int[] v = new int[Long.SIZE];
        for (java.util.Map.Entry<Long, Integer> e : freq.entrySet()) {
            long h = e.getKey();
            int w = e.getValue();
            for (int b = 0; b < Long.SIZE; b++) {
                v[b] += ((h >>> b) & 1L) == 1L ? w : -w;
            }
        }
        long out = 0L;
        for (int b = 0; b < Long.SIZE; b++) {
            if (v[b] > 0) {
                out |= (1L << b);
            }
        }
        return out;
    }

    /** 64 位切成 {@link #BANDS} 段，每段 {@link #BAND_BITS} 位（低位在前）。 */
    public static long[] bands(long hash) {
        long[] out = new long[BANDS];
        for (int i = 0; i < BANDS; i++) {
            out[i] = (hash >>> (i * BAND_BITS)) & 0xFFFFL;
        }
        return out;
    }

    /** 汉明距离（不同位的个数）。 */
    public static int hamming(long a, long b) {
        return Long.bitCount(a ^ b);
    }

    /** FNV-1a 64。逐 UTF-16 码元 —— 常用汉字都在 BMP，够用；扩展区会被拆成两个码元但**仍然确定**。 */
    private static long hash64(String s) {
        long h = FNV_OFFSET;
        for (int i = 0; i < s.length(); i++) {
            h ^= s.charAt(i);
            h *= FNV_PRIME;
        }
        return h;
    }
}
