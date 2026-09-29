# -*- coding: utf-8 -*-
"""
**「结构性句子」还剩多少躲过 `sent-no-head`** —— 待办 ⑧ 的取证。

现状（`SentenceMapper` 的 `noHead` 分支）只有两条规则：
    kind <> 'head'  AND  NOT (text LIKE '%目录%' AND length(text) > 80)
而 2026-09-29 实测到**同一行在正文里写两遍**（那是标题重复行）：`kind='sent'`、
不含「目录」⇒ **两条规则一条都拦不住**，可它**关键词密集 ⇒ 相似度天然高** ⇒
挤占材料名额。

⚠️ 本探针**先量再改**（规矩：判据/过滤要按分布改，不拍）。判据落在**结构**上
（"这行是不是文档名/标题"、"这行在不在同一篇里重复出现"），不落在相似度上。

用法：python tools/struct-sent-probe.py
"""
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402


def q(sql):
    return corpus.psql_rows(sql)


def bar(n, mx, width=40):
    return "█" * max(0, min(width, int(round(n * width / mx)) if mx else 0))


def main():
    print("=" * 74)
    print("一、现在 `noHead` 拦掉多少（分母先立住）")
    print("=" * 74)
    tot = int(q("SELECT count(*) FROM sentences")[0][0])
    head = int(q("SELECT count(*) FROM sentences WHERE kind = 'head'")[0][0])
    cat = int(q("SELECT count(*) FROM sentences WHERE kind <> 'head' "
                "AND text LIKE '%目录%' AND length(text) > 80")[0][0])
    print(f"  sentences 合计 {tot}")
    print(f"  kind='head'              {head:>6}  ({head/tot*100:.2f}%)")
    print(f"  含「目录」且 >80 字        {cat:>6}  ({cat/tot*100:.2f}%)")
    print(f"  ⇒ 现在共拦 {head+cat} 条（{(head+cat)/tot*100:.2f}%）")
    rows = q("SELECT kind, count(*) FROM sentences GROUP BY 1 ORDER BY 2 DESC")
    print("  kind 分布：" + " · ".join(f"{r[0]}={r[1]}" for r in rows if r and r[0]))

    print("\n" + "=" * 74)
    print("二、**同一篇里出现两次以上的句子**（「标题重复行」的机械判据）")
    print("=" * 74)
    dup = q("""
        SELECT count(*), count(DISTINCT doc_id) FROM (
          SELECT doc_id, text FROM sentences
          WHERE kind <> 'head'
          GROUP BY doc_id, text HAVING count(*) > 1) t""")
    n_dup, n_doc = (int(dup[0][0]), int(dup[0][1])) if dup and dup[0] else (0, 0)
    print(f"  **篇内重复**的句子文本：{n_dup} 条（涉及 {n_doc} 篇）")
    print(f"  占 sentences 的 {n_dup/tot*100:.2f}%")
    rows = q("""
        SELECT left(text, 46), count(*) c FROM sentences
        WHERE kind <> 'head'
        GROUP BY doc_id, text HAVING count(*) > 1
        ORDER BY c DESC, 1 LIMIT 15""")
    print(f"  {'重复行（前 46 字）':<50}{'次数':>5}")
    for r in rows:
        if r and r[0]:
            print(f"  {r[0]:<50}{r[1]:>5}")

    print("\n" + "=" * 74)
    print("三、**正文里重复的那行是不是文档名/标题**（判断它「结构性」的依据）")
    print("=" * 74)
    rows = q("""
        SELECT d.name, s.text, count(*) OVER (PARTITION BY s.doc_id, s.text) c
        FROM sentences s JOIN documents d ON d.id = s.doc_id
        WHERE s.kind <> 'head'
          AND length(s.text) BETWEEN 6 AND 60
          AND (d.name LIKE '%' || s.text || '%' OR s.text LIKE '%' || d.name || '%')
        LIMIT 20""")
    if not rows or rows == [[""]]:
        print("  （没有「句子与文档名互相包含」的 —— 说明文档名不是标题）")
    else:
        for r in rows:
            if r and r[0]:
                print(f"  文档名：{r[0][:40]}")
                print(f"    句  ：{r[1][:60]}   （同篇出现 {r[2]} 次）")

    print("\n" + "=" * 74)
    print("四、**这些行有没有挤进过材料**（拿生产日志的实测，不是推测）")
    print("=" * 74)
    print("  ⚠️ 这一节要的是「它排第几」，得拿问题向量算 —— 本探针只做全库形状，")
    print("     名次留给 `bench-material-probe` 的逐题读数（它会打出每题剩几句）")
    print("  ⚠️ **还没改任何东西**：先看上面三节的量级再决定动不动手")

    print("\n" + "=" * 74)
    print("五、样本：几篇 feed 文档的句子长什么样（人眼核对「结构性」长什么样）")
    print("=" * 74)
    rows = q("""
        SELECT d.name, s.seq, s.kind, left(s.text, 54)
        FROM sentences s JOIN documents d ON d.id = s.doc_id
        WHERE d.source_kind = 'feed'
        ORDER BY s.doc_id, s.seq LIMIT 26""")
    last = None
    for r in rows:
        if not r or not r[0]:
            continue
        if r[0] != last:
            print(f"  ── {r[0][:56]}")
            last = r[0]
        print(f"     [{r[1]:>2}] {r[2]:<5} {r[3]}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
