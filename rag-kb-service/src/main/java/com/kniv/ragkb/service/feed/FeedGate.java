package com.kniv.ragkb.service.feed;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Locale;

/**
 * **入库准入闸 0**：纯规则、零 GPU、零模型调用。
 *
 * <p><b>它判的是"可见性"，不是"存亡"</b> —— 这是这一支的设计原则：
 * 判定的结果写进 {@code feed_items.status}，行**一律留下**。
 * 三条理由（写在子台账里，这里再记一遍因为最容易违反）：
 * <ol>
 *   <li>规则与权重都会估错，而**丢弃是不可逆的**；</li>
 *   <li>校准来源权重（某来源的报道被后续推翻多少次）**需要保留本该丢的样本**；</li>
 *   <li>存储便宜、召回筛选贵。</li>
 * </ol>
 *
 * <p>三层闸里这是第 0 层（最便宜的）。第 1 层是"这条有没有可陈述的事实主张"（一次短前向），
 * 第 2 层是 `权重 = f(来源) × g(对象) × h(可陈述性)` 过阈值 —— 那两层需要模型，
 * 而且**阈值要用数据定**，所以先只做这一层，跑几天拿到分布再说。
 */
public final class FeedGate {

    /** 可见性档位，与 {@code feed_items.status} 一一对应。 */
    public enum Status {
        /** 冷存：留下但不进召回池 */
        COLD(0),
        /** 可召回 */
        RECALLABLE(1),
        /** 已转成文档进知识库 */
        PROMOTED(2);

        private final int code;

        Status(int code) {
            this.code = code;
        }

        public int code() {
            return code;
        }
    }

    /** 判定结果。理由**必须带出去落库** —— 不记的话"为什么这条没召回"只能靠猜。 */
    public record Verdict(Status status, String reason) {
    }

    /** 参数。默认值都是"先宽后紧"：宁可先放进来（冷存），也别在第一版就把数据丢掉。 */
    public record Config(int minChars, double minCjkRatio, Duration maxAge, List<String> blockedDomains,
                         int listingMinChars, double listingMaxDensity) {

        public static Config defaults() {
            return new Config(
                    200,                                  // 正文短于 200 字：多为导航页/JS 壳/反爬页
                    0.30,                                 // 中日韩字符占比低于三成
                    Duration.ofDays(30),                  // 抓到的旧闻：冷存，不直接进召回池
                    List.of("localhost", "127.0.0.1"),    // 域名黑名单（内网地址由 SSRF 检查兜底）
                    1500, 1.0);                           // 列表页判据（见 check 里的返工记录）
        }
    }

    private FeedGate() {
    }

    /**
     * 过闸。
     *
     * @param body        正文（已抽干净的 Markdown/纯文本）
     * @param domain      来源域名（小写，不含 www.）
     * @param publishedAt 发布时间，可能为 null（很多页不给）
     * @param now         当前时间（显式传入，便于回放与测试）
     */
    public static Verdict check(String body, String domain, Instant publishedAt, Instant now, Config cfg) {
        String text = body == null ? "" : body.strip();
        if (text.length() < cfg.minChars()) {
            return new Verdict(Status.COLD, "太短：" + text.length() + " 字（阈值 " + cfg.minChars() + "）");
        }
        double cjk = cjkRatio(text);
        if (cjk < cfg.minCjkRatio()) {
            return new Verdict(Status.COLD,
                    "非中文为主：CJK 占比 " + String.format(Locale.ROOT, "%.2f", cjk));
        }
        if (domain != null && cfg.blockedDomains().contains(domain.toLowerCase(Locale.ROOT))) {
            return new Verdict(Status.COLD, "域名在黑名单：" + domain);
        }
        // ⚠️ **这里曾经有一条"列表页"判据，已回退**（2026-09-25）：
        //
        //   `正文 > 1500 字 且 句号密度 < 3/千字 ⇒ 冷存`
        //
        // 它在**手挑的 10 个样本**上是 10/10（那 10 条确实全是门户/列表页），
        // 于是当时判它"精度 10/10"。**但跑进真实管线之后它只命中过 2 次，两次都是真文章**：
        //   李强出席全球数字贸易博览会致辞 / 李强会见马来西亚总理安瓦尔 ——
        //   官方通稿句式短、句号少（密度 1.8），于是被当成列表页。
        //
        // 两类实际的密度分布**是重叠的**：真列表页 0.0~2.6，通稿 1.8 —— **根本分不开**。
        // 教训（比这条判据值钱）：**判据要在"真实管线产生的数据"上量，不能只在手挑的样本上量**；
        // 手挑样本里没有的那一类，就是判据的盲区。
        //
        // 门户列表页现在唯一的人口是"手动搜索→喂网址"那条路（RSS 抓的都是文章页），
        // 数量很少；真要治它，得靠**链接密度**（列表页满是 <a>）—— 而那要求在抽取层保留
        // 链接计数，是另一个改动，等有真实需求再说。

        // **列表页/门户页**：长，但几乎没有句子。
        //
        // ⚠️ 这条判据**返工过一次**，两次的差别只在阈值（记下来，因为过程比结论值钱）：
        //   · 第一版卡 `< 3/千字`，在**手挑的 10 个样本**上是 10/10 —— 于是当时写了"精度 10/10"；
        //     跑进真实管线后**只命中 2 次，两次都是真文章**（李强通稿句式短、句号少，密度 1.8）。
        //     两类分布**重叠**（真列表页 0.0~2.6 / 通稿 1.8）⇒ 判据被回退。
        //   · 现在卡 `< 1.0/千字`，是在**真实管线全部 718 条**上量的：
        //     **6 条命中、6 条全是列表页、0 条误伤**（4 个门户首页密度 0.0/0.6/0.0/0.0，
        //     另 2 条是「台风路径实时发布系统」与「中新网滚动新闻」——也是列表页）。
        //     **通稿那两条（1.8）安全落在阈值之上。**
        // ⇒ 教训：**判据要在真实管线的数据上量**；手挑样本里没有的那一类，就是判据的盲区。
        //
        // 只判 **COLD**（可见性）—— 万一误判，行还在、可回捞。
        int n = text.length();
        if (n > cfg.listingMinChars()) {
            double density = (countChar(text, '。') + countChar(text, '；')) * 1000.0 / n;
            if (density < cfg.listingMaxDensity()) {
                return new Verdict(Status.COLD, "像列表页：正文 " + n + " 字但句号密度只有 "
                        + String.format(Locale.ROOT, "%.1f", density) + "/千字（阈值 "
                        + cfg.listingMaxDensity() + "）");
            }
        }

        if (publishedAt != null && now != null
                && publishedAt.isBefore(now.minus(cfg.maxAge()))) {
            long days = Duration.between(publishedAt, now).toDays();
            return new Verdict(Status.COLD, "已 " + days + " 天前的旧闻（阈值 " + cfg.maxAge().toDays() + " 天）");
        }
        // 没给发布时间的不当旧闻处理：很多站点不给，卡在这里会把新内容也挡掉。
        // 时间维度的分量留给排序（下一步），不在这里做否决。
        return new Verdict(Status.RECALLABLE,
                publishedAt == null ? "过闸（无发布时间）" : "过闸");
    }

    /**
     * 中日韩字符占非空白字符的比例。
     *
     * <p>不看字符总数而看**占比**：新闻页常夹大量英文标签、导航与广告串，
     * 只看"有没有中文"会把英文站也放进来。
     */
    public static double cjkRatio(String text) {
        int total = 0;
        int cjk = 0;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (Character.isWhitespace(c)) {
                continue;
            }
            total++;
            if (isCjk(c)) {
                cjk++;
            }
        }
        return total == 0 ? 0 : (double) cjk / total;
    }

    private static int countChar(String s, char c) {
        int n = 0;
        for (int i = 0; i < s.length(); i++) {
            if (s.charAt(i) == c) {
                n++;
            }
        }
        return n;
    }

    /** 汉字（含扩展 A）＋ 中文标点区间。够用即可 —— 这里判的是"是不是中文内容"。 */
    private static boolean isCjk(char c) {
        return (c >= 0x4E00 && c <= 0x9FFF)      // 基本汉字
                || (c >= 0x3400 && c <= 0x4DBF)  // 扩展 A
                || (c >= 0x3000 && c <= 0x303F)  // 中文标点
                || (c >= 0xFF00 && c <= 0xFFEF); // 全角
    }
}
