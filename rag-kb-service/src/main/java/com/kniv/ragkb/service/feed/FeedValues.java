package com.kniv.ragkb.service.feed;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;

/**
 * **从 MyBatis 返回的 {@code Map} 里安全取值的唯一入口**。
 *
 * <p>为什么值得单独一个类（这一支已经被咬了**三次**）：
 * <ol>
 *   <li>{@code AS itemN} 被 PostgreSQL 折成小写 ⇒ map 键是 {@code itemn} ⇒ 取到 null；</li>
 *   <li>同一个坑换成 {@code AS fTitle} 又来一次 ⇒ 60 条条目全被丢掉，报"feed 空"；</li>
 *   <li>{@code timestamptz} 列取出来是 {@link Timestamp} 而代码强转 {@link OffsetDateTime}
 *       ⇒ 抛 ClassCastException（晋升那一步八条全失败）。</li>
 * </ol>
 *
 * <p>前两次的教训是"SQL 别名一律加引号"，第三次的教训是"**列的 Java 类型也别假设**"——
 * 而三次的共同点是：**取值这件事散在各处、每处各自假设**。
 * 所以收成一个入口：**取值只走这里，别在调用点强转**。
 *
 * <p>⚠️ 注意这里**不吞异常、也不猜**：类型不认识就返回 null（时间字段为 null 是合法状态，
 * 表示"没拿到发布时间"，与"拿到了 1970 年"完全不是一回事）。
 */
public final class FeedValues {

    private FeedValues() {
    }

    public static String str(Object o) {
        return o == null ? "" : o.toString();
    }

    /** timestamptz 可能是 Timestamp / OffsetDateTime / Instant —— 三种都认。 */
    public static OffsetDateTime time(Object o) {
        Instant i = instant(o);
        return i == null ? null : i.atZone(ZoneId.systemDefault()).toOffsetDateTime();
    }

    public static Instant instant(Object o) {
        if (o == null) {
            return null;
        }
        if (o instanceof Timestamp ts) {
            return ts.toInstant();
        }
        if (o instanceof OffsetDateTime odt) {
            return odt.toInstant();
        }
        if (o instanceof Instant i) {
            return i;
        }
        if (o instanceof java.util.Date d) {
            return d.toInstant();
        }
        return null;
    }

    public static long num(Object o, long fallback) {
        return o instanceof Number n ? n.longValue() : fallback;
    }

    public static int intOf(Object o, int fallback) {
        return o instanceof Number n ? n.intValue() : fallback;
    }
}
