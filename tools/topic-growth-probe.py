# -*- coding: utf-8 -*-
"""
**议题层增长探针** —— 回答一个问题：议题数**为什么**在长，以及它是不是线性于条目。

背景（2026-09-29 用户提问）：条目 2928→3644（+25%）的同时议题 1578→2104。
"再加一层大类"能不能治，取决于**增长是哪个机制造出来的**。所以这里量的是机制：

  ① 规模分布        —— 议题层是"事件簇"还是"条目的一份副本"（单条占比）
  ② 增长曲线        —— 议题/条目/晋升 各自的日增，看它们是不是同一条线
  ③ **并入 vs 新建** —— 每次富化落在哪一边（这是增长率的直接来源）
  ④ **重复命名**     —— 同一个名字被起了几次 ⇒ 时间窗(7天)有没有把一件事**切成多个议题**
  ⑤ 稳定轴的候选     —— 频道/来源/日期 的新值率（"极少膨胀"的那一层长什么样）

用法：python tools/topic-growth-probe.py
"""
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402


def q(sql):
    return corpus.psql_rows(sql)


def one(sql, default=0):
    r = q(sql)
    if not r or not r[0]:
        return default
    try:
        return int(r[0][0])
    except (ValueError, TypeError):
        return default


def bar(n, mx, width=50):
    return "█" * max(0, min(width, int(round(n * width / mx))) if mx else 0)


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def main():
    section("〇、总量")
    items = one("SELECT count(*) FROM feed_items")
    topics = one("SELECT count(*) FROM feed_topics")
    edges = one("SELECT count(*) FROM feed_item_topics")
    labeled = one("SELECT count(*) FROM feed_topics WHERE label IS NOT NULL")
    promoted = one("SELECT count(*) FROM feed_items WHERE doc_id IS NOT NULL")
    srcs = one("SELECT count(*) FROM feed_sources")
    chans = one("SELECT count(*) FROM feed_channels WHERE enabled")
    print(f"  条目 {items} · 议题 {topics}（已命名 {labeled}）· 边 {edges}")
    print(f"  已晋升 {promoted} · 来源 {srcs} · 启用频道 {chans}")
    if items and topics:
        print(f"  ⇒ **条目/议题 = {items/topics:.2f}**（=1.00 就是「每个议题一条」，"
              f"越大才越像「事件簇」）")

    section("一、议题规模分布（**这一节回答「议题层是不是条目的副本」**）")
    rows = q("SELECT item_n, count(*) FROM feed_topics GROUP BY 1 ORDER BY 1")
    dist = {int(r[0]): int(r[1]) for r in rows}
    mx = max(dist.values()) if dist else 1
    tot = sum(dist.values()) or 1
    single = dist.get(1, 0)
    print(f"  {'规模':>6} {'议题数':>7} {'占比':>7}  分布")
    for k in sorted(dist):
        if k <= 5 or k % 5 == 0 or k > 20:
            print(f"  {k:>6} {dist[k]:>7} {dist[k]/tot*100:>6.1f}%  {bar(dist[k], mx)}")
    print(f"  ⇒ **单条议题 {single}/{tot} = {single/tot*100:.1f}%**")
    print(f"  ⇒ 多条（≥2，够晋升门槛）{tot-single} 个，占 {(tot-single)/tot*100:.1f}%")
    multi_items = sum(k * v for k, v in dist.items()) - single
    print(f"  ⇒ 落在「多条议题」里的条目 {multi_items}/{items} = {multi_items/max(items,1)*100:.1f}%"
          f"（其余是**只被一家报道过**的事件）")

    section("二、增长曲线（日）")
    print("  （议题按 first_seen，条目按 fetched_at；晋升没有时间列，看不了日增）")
    ti = q("SELECT to_char(date_trunc('day', first_seen), 'MM-DD'), count(*) "
           "FROM feed_topics GROUP BY 1 ORDER BY 1")
    ii = q("SELECT to_char(date_trunc('day', fetched_at), 'MM-DD'), count(*) "
           "FROM feed_items GROUP BY 1 ORDER BY 1")
    ei = q("SELECT to_char(date_trunc('day', joined_at), 'MM-DD'), count(*) "
           "FROM feed_item_topics GROUP BY 1 ORDER BY 1")
    d_i = {r[0]: int(r[1]) for r in ii}
    d_t = {r[0]: int(r[1]) for r in ti}
    d_e = {r[0]: int(r[1]) for r in ei}
    days = sorted(set(d_i) | set(d_t) | set(d_e))
    mx = max(list(d_i.values()) + [1])
    print(f"  {'日期':>6} {'条目':>6} {'议题':>6} {'边':>6}  {'新议题/条目':>10}  条目分布")
    for d in days:
        n_i, n_t, n_e = d_i.get(d, 0), d_t.get(d, 0), d_e.get(d, 0)
        ratio = f"{n_t/n_i:.2f}" if n_i else "-"
        print(f"  {d:>6} {n_i:>6} {n_t:>6} {n_e:>6}  {ratio:>10}  {bar(n_i, mx, 30)}")

    section("三、并入 vs 新建 —— **增长率的直接来源**")
    print("  边上 sim=1.0 是**新建**时写死的（见 FeedEnrichService：linkItemTopic(id, topic, 1.0f)）")
    rows = q("SELECT width_bucket(sim::float8, 0.5, 1.0, 10), count(*) "
             "FROM feed_item_topics GROUP BY 1 ORDER BY 1")
    h = {int(r[0]): int(r[1]) for r in rows}
    mx = max(h.values()) if h else 1
    tot_e = sum(h.values()) or 1
    print(f"  {'桶':>4} {'边数':>7} {'占比':>7}  分布")
    for b in sorted(h):
        lo = 0.5 + (b - 1) * 0.05
        print(f"  {lo:.2f}~{lo+0.05:.2f} {h[b]:>7} {h[b]/tot_e*100:>6.1f}%  {bar(h[b], mx)}")
    print("  ⚠️ 最后一档是**上界溢出桶**（sim 正好 ≥1.0），里面**混着「新建」与「并进质心正好相同」**，")
    print("     两者在这一列上不可分 —— 要分开得看 feed_items.nearest_topic_sim 有没有值")

    section("四、**同一个名字被起了几次** ⇒ 时间窗把一件事切成了几个议题？")
    rows = q("SELECT label, count(*) c FROM feed_topics WHERE label IS NOT NULL "
             "GROUP BY 1 HAVING count(*) > 1 ORDER BY c DESC, label LIMIT 25")
    if not rows or rows == [[""]]:
        print("  （没有重名 —— 逐字重名一个都没有）")
    else:
        print(f"  {'名字':<20} {'议题数':>6}")
        for r in rows:
            if len(r) >= 2 and r[0]:
                print(f"  {r[0]:<20} {r[1]:>6}")
    dupn = one("SELECT count(*) FROM (SELECT label FROM feed_topics WHERE label IS NOT NULL "
               "GROUP BY 1 HAVING count(*) > 1) t")
    named = one("SELECT count(DISTINCT label) FROM feed_topics WHERE label IS NOT NULL")
    print(f"  ⇒ 逐字重名的名字 {dupn} 个（占已命名名字 {named} 的 "
          f"{dupn/max(named,1)*100:.1f}%）")
    print("  ⚠️ 逐字重名只是**下界**：同一个事件被换个说法命名（「台风X登陆」vs「X台风登陆浙江」）")
    print("     在这里看不出来。要看全貌得比标签的**字符级相似度**。")

    section("五、稳定轴的候选：各自的新值率")
    print("  " + f"{'轴':<16}{'当前值数':>9}{'近7天新增':>10}{'近7天条目':>10}{'新值/条目':>10}")
    axes = [
        ("频道 channel", "SELECT count(*) FROM feed_channels WHERE enabled",
         "SELECT count(*) FROM feed_channels WHERE enabled AND id > (SELECT max(id)-13 FROM feed_channels)"),
        ("来源 source", "SELECT count(*) FROM feed_sources",
         "SELECT count(*) FROM feed_sources WHERE created_at > now() - interval '7 day'"),
        ("议题 topic", "SELECT count(*) FROM feed_topics",
         "SELECT count(*) FROM feed_topics WHERE first_seen > now() - interval '7 day'"),
        ("日期 day", "SELECT count(DISTINCT date_trunc('day', fetched_at)) FROM feed_items",
         "SELECT 7"),
    ]
    n7 = one("SELECT count(*) FROM feed_items WHERE fetched_at > now() - interval '7 day'")
    for name, cur, new in axes:
        c, nw = one(cur), one(new)
        r = f"{nw/n7:.3f}" if n7 else "-"
        print(f"  {name:<16}{c:>9}{nw:>10}{n7:>10}{r:>10}")
    print("  ⇒ 这张表要看的**不是值数，是「每来一条新条目，这一轴要添几个新值」**")
    print("     零 ⇒ 这一轴**不随语料膨胀**（分类表的定义性特征）")

    section("五之二、**活跃集**：把「累计」和「现在还有用」分开")
    alive7 = one("SELECT count(*) FROM feed_topics WHERE last_seen > now() - interval '7 day'")
    alive1 = one("SELECT count(*) FROM feed_topics WHERE last_seen > now() - interval '1 day'")
    dead = one("SELECT count(*) FROM feed_topics WHERE last_seen <= now() - interval '7 day'")
    dead_items = one("SELECT count(DISTINCT it.item_id) FROM feed_item_topics it JOIN feed_topics t "
                     "ON t.id = it.topic_id WHERE t.last_seen <= now() - interval '7 day'")
    print(f"  累计议题            {topics:>6}")
    print(f"  近 1 天有动静        {alive1:>6}")
    print(f"  近 7 天有动静        {alive7:>6}  ← **这才是「当前有话可说的议题」**")
    print(f"  7 天前就没动静了      {dead:>6}  （占 {dead/max(topics,1)*100:.1f}%，"
          f"覆盖 {dead_items} 条条目）")
    print("  ⇒ **累计在长、活跃不一定要长**。活跃集的稳态大小 = 日增议题 × 存活的平均天数。")
    print("     所以「不膨胀」的正解是**让老议题退休**，不是在上头再加一层。")

    section("五之三、一条条目的『时间桶』稳定性")
    n_day = one("SELECT count(DISTINCT date_trunc('day', fetched_at)) FROM feed_items")
    print(f"  有数据的自然日 {n_day} 天 · 日均条目 {items/max(n_day,1):.0f} · "
          f"日均新议题 {topics/max(n_day,1):.0f}")
    print("  日期/周/月 是**唯一一个天生不膨胀、且语义不漂**的轴（新闻本来就按时间读）")

    section("六、议题的时间跨度：**同一个议题活了多久**")
    rows = q("SELECT width_bucket(EXTRACT(epoch FROM (last_seen - first_seen))/86400.0, 0, 14, 7), "
             "count(*) FROM feed_topics GROUP BY 1 ORDER BY 1")
    h = {int(r[0]): int(r[1]) for r in rows}
    mx = max(h.values()) if h else 1
    for b in sorted(h):
        lo = (b - 1) * 2
        print(f"  {lo:>2}~{lo+2:>2} 天 {h[b]:>6}  {bar(h[b], mx)}")
    print("  ⚠️ 时间窗是 7 天 —— 若这一列的分布**紧贴 0**，说明议题几乎都是当天生当天死；")
    print("     若有明显的一条 7 天附近的尾巴，说明**同一件事被窗切断过**（第七节验）")

    section("七、活跃议题的时间线（看长期事件是不是被切成多个议题）")
    rows = q("SELECT id, label, item_n, to_char(first_seen,'MM-DD HH24:MI'), "
             "to_char(last_seen,'MM-DD HH24:MI'), "
             "round(EXTRACT(epoch FROM (last_seen-first_seen))/3600.0) "
             "FROM feed_topics ORDER BY item_n DESC LIMIT 20")
    print(f"  {'id':>6} {'名字':<22}{'条':>4} {'first':>12} {'last':>12} {'跨时(h)':>8}")
    for r in rows:
        if len(r) < 6:
            continue
        print(f"  {r[0]:>6} {(r[1] or '(未命名)')[:22]:<22}{r[2]:>4} {r[3]:>12} {r[4]:>12} {r[5]:>8}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
