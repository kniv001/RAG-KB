# -*- coding: utf-8 -*-
"""
**信息流方向的三把尺子** —— 全部是"校准阈值"用的分布，不是判定结果。

为什么单独一个脚本而不是只在应用里打日志：这一支有**三个阈值**要靠数据定
（`Simhash.DUP_DISTANCE` / `feed.dup-sim` / `feed.topic-sim`），而每个阈值都有一列
专门的仪器托着（`nearest_dist` / `feed_segments.best_sim` / `nearest_topic_sim`）。
尺子和阈值**成对出现**，看分布的时候要一起看，所以放一处。

用法：
    python tools/feed-ruler.py
    python tools/feed-ruler.py --items 20      # 顺带列条目
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402


def hist(sql, lo, hi, buckets, label, null_label=None, null_sql=None):
    print(f"\n{label}")
    rows = corpus.psql_rows(sql)
    if not rows or rows == [[""]]:
        print("  （空）")
        return
    width = (hi - lo) / buckets
    for r in rows:
        b = int(r[0])
        n = int(r[1])
        start = lo + (b - 1) * width
        end = start + width
        bar = "█" * min(60, n)
        print(f"  {start:.2f}~{end:.2f} {n:>5}  {bar}")
    if null_sql:
        n = int(corpus.psql_rows(null_sql)[0][0])
        print(f"  {null_label or '（无候选）'}：{n}")
    print("  ⚠️ 最后一档是 width_bucket 的**上界溢出桶**：里面是**正好等于上界**的值，"
          "不是「超过上界」（第一版把它读成 >1.0 的相似度，自己吓自己一次）")


def main():
    print("=" * 72)
    print("一、整篇指纹：与最近候选的汉明距离（校准 Simhash.DUP_DISTANCE）")
    print("=" * 72)
    hist("SELECT width_bucket(nearest_dist::float8, 0, 64, 16), count(*) FROM feed_items "
         "WHERE nearest_dist IS NOT NULL GROUP BY 1 ORDER BY 1",
         0, 64, 16, "汉明距离（0 = 逐字相同；≤3 判为重复）",
         "连候选都没有（LSH 盲区，与「距离很大」不是一回事）",
         "SELECT count(*) FROM feed_items WHERE nearest_dist IS NULL AND status = 1")

    print("\n" + "=" * 72)
    print("二、段落级：这段内容库里最像的有多像（校准 feed.dup-sim）")
    print("=" * 72)
    hist("SELECT width_bucket(best_sim::float8, 0.5, 1.0, 10), count(*) FROM feed_segments "
         "WHERE best_sim IS NOT NULL GROUP BY 1 ORDER BY 1",
         0.5, 1.0, 10, "余弦相似度（阈值越靠右，判重复越保守）",
         "库里还没有可比段落（第一批条目必然如此）",
         "SELECT count(*) FROM feed_segments WHERE best_sim IS NULL")

    print("\n" + "=" * 72)
    print("三、议题级：条目与最近议题质心的相似度（校准 feed.topic-sim）")
    print("=" * 72)
    hist("SELECT width_bucket(nearest_topic_sim::float8, 0.5, 1.0, 10), count(*) FROM feed_items "
         "WHERE nearest_topic_sim IS NOT NULL GROUP BY 1 ORDER BY 1",
         0.5, 1.0, 10, "余弦相似度",
         "没有可比议题（时间窗内没有议题）",
         "SELECT count(*) FROM feed_items WHERE nearest_topic_sim IS NULL AND status = 1")

    print("\n" + "=" * 72)
    print("四、议题榜")
    print("=" * 72)
    for r in corpus.psql_rows(
            "SELECT t.id, count(*) AS n, round(avg(e.sim)::numeric, 3) AS sim, "
            "min(i.title) AS sample FROM feed_topics t "
            "JOIN feed_item_topics e ON e.topic_id = t.id "
            "JOIN feed_items i ON i.id = e.item_id "
            "GROUP BY t.id ORDER BY n DESC, t.id LIMIT 20"):
        print(f"  议题 {r[0]:>4}  条目 {r[1]:>3}  平均相似 {r[2]}  「{(r[3] or '')[:44]}」")

    print("\n" + "=" * 72)
    print("五、总数")
    print("=" * 72)
    for r in corpus.psql_rows(
            "SELECT (SELECT count(*) FROM feed_items), "
            "(SELECT count(*) FROM feed_items WHERE status = 1), "
            "(SELECT count(*) FROM feed_segments), "
            "(SELECT count(*) FROM feed_topics), "
            "(SELECT count(DISTINCT source_id) FROM feed_items)"):
        print(f"  条目 {r[0]}（可召回 {r[1]}）· 段落 {r[2]} · 议题 {r[3]} · 来源 {r[4]}")


if __name__ == "__main__":
    main()
