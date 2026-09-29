# -*- coding: utf-8 -*-
"""
**议题名字的护栏该定在哪** —— 待办 13② 的取证与定档。

已结案的部分（不需要本探针）：那 10 个坏名字**不是管线造的**。
`FeedTopicLabelService` 全程只做三件事 —— `strip()`、`usable()`、`substring(0,16)`；
`JsonExtract.firstObject` 只**定位**对象、解码交给 Jackson。`substring` 只可能**截断**，
**没有任何一条路径能把「克」换成 `:`** ⇒ 是**模型输出的字符级错误**（0.5%）。

本探针只回答"护栏定在哪"：**合法名字与来源标题的最长公共子串（LCS）有多长**。
   · 若合法名字普遍有很长的公共子串 ⇒ 拿"必须有一段 ≥N 字连续公共子串"当护栏**不误伤**
   · 那 10 个坏名字应当**明显低于**合法分布（它们各有一处被 `:` 顶掉 ⇒ 断了连续段）

⚠️ 判据要落在**分布**上、不落在"我碰巧看到的那几个"上（本项目的旧账）。
用法：python tools/label-guard-probe.py
"""
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402


def lcs_len(a, b):
    """最长公共子串长度（滚动数组；a/b 都很短）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def main():
    rows = corpus.psql_rows("""
        SELECT t.id, coalesce(t.label,''), t.item_n,
               string_agg(coalesce(i.title,''), chr(1))
        FROM feed_topics t
        JOIN feed_item_topics it ON it.topic_id = t.id
        JOIN feed_items i ON i.id = it.item_id
        WHERE t.label IS NOT NULL
        GROUP BY t.id, t.label, t.item_n""")
    data = []
    for r in rows:
        if len(r) < 4 or not r[0] or not r[1]:
            continue
        data.append((int(r[0]), r[1], int(r[2]), r[3].split(chr(1))))
    print("=" * 74)
    print(f"已命名议题 {len(data)} 个（每个都带它成员的标题）")
    print("=" * 74)

    BAD = {1342, 1387, 1413, 784, 1520, 1810, 1910, 2254, 2432, 1633, 2124, 2226}
    dist = Counter()
    bad_vals = []
    for tid, lab, n, titles in data:
        best = max((lcs_len(lab, t) for t in titles if t), default=0)
        dist[best] += 1
        if tid in BAD:
            bad_vals.append((tid, lab, best))

    print("\n一、**合法名字与来源标题的最长公共子串**（越长越「确实来自标题」）")
    print(f"  {'LCS':>4}{'议题数':>7}{'占比':>8}{'累计':>8}   ")
    tot = sum(dist.values())
    cum = 0
    for k in sorted(dist):
        cum += dist[k]
        mark = "  ← 护栏候选" if k <= 4 else ""
        print(f"  {k:>4}{dist[k]:>7}{dist[k]/tot*100:>7.1f}%{cum/tot*100:>7.1f}%{mark}")
    print(f"  ⇒ 中位 **{sorted([k for k in dist for _ in range(dist[k])])[len(data)//2]}** 字")

    print("\n二、**那批坏名字落在这条分布上的哪里**")
    print(f"  {'id':>5} {'名字':<26}{'LCS':>5}")
    for tid, lab, best in sorted(bad_vals, key=lambda x: x[2]):
        print(f"  {tid:>5} {lab[:26]:<26}{best:>5}")
    print("  ⇒ 若它们**整体落在分布的左尾**，护栏就分得开；若混在中间，护栏会误伤。")

    print("\n三、护栏候选的**误伤面**（拿不同阈值算：会被拒掉的合法名字有多少）")
    for th in (4, 5, 6, 8):
        rej = sum(v for k, v in dist.items() if k < th)
        caught = sum(1 for _, _, b in bad_vals if b < th)
        print(f"  LCS < {th} 就拒：拒掉 {rej:>4} 个（{rej/tot*100:.1f}%）· "
              f"坏名字抓到 {caught}/{len(bad_vals)}")
    print("  ⚠️ **本探针只给分布，不下结论** —— 阈值要与「误伤掉的那几个是什么」一起看。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
