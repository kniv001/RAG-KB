# -*- coding: utf-8 -*-
"""
**入口键（跨议题共享词）** —— 一份实现，两处用：回放探针 + 每日曲线。

## 它是什么，以及为什么是这个方向（2026-09-29 量出来的）

用户提的是「多个主语组成的复合键当议题的唯一性判别键」。量下来**方向很关键**：

  · **从议题自己抽**（签名 = 该议题过半成员共有的词）⇒ 抽出来的是**这个议题的指纹**，
    压缩比 **1.2~1.6 : 1**（412 议题 → 322 入口），扫遍松紧、关掉家具过滤都不动。
    ⇒ **这条路不成立**，而且不是参数问题，是方向反了。
  · **从全库抽**（先找覆盖 5~60 个议题的词，再让议题挂上去）⇒
    **57 个入口 / 最大挂 16 个议题 / 覆盖 39%**。⇒ **成立。**

⚠️ **必须封顶**（`topk`）：今晨量过，同一批词走**传递闭包**能压到 47 个入口，
但**最大入口 1870/1923**（全库一坨）。**"压得动"与"不串"只在封了顶之后同时成立。**

⚠️ **家具词按分布定，不手写**：手写那版把「界面新闻」漏成了最大撞车主语（25 个议题撞一起）；
但**纯按频率也不够** —— 2% 的阈值会把「亚运」「中秋」「习近平」这些**真实体**也滤掉
（**"热事件"与"站点名"在词频上分不开**）。⇒ 这里的阈值是**折中**，换语料要重看。
"""
import re
from collections import Counter, defaultdict

from . import corpus

CJK = re.compile(r"[一-鿿]+")

# 入口词覆盖多少个**议题**才算入口。取了这一段：
#   下界 5 —— 少于 5 个议题共享的词，当入口太碎（实测 3~60 会让入口涨到 248、逼近议题数）
#   上界 60 —— 覆盖太广的词是"语域"不是"这件事"（实测去掉上界基本无差别，留着是防线）
LO, HI = 5, 60
TOPK = 3          # 一个议题最多挂几个入口 —— **封顶就是"不串"的全部理由**
FURNITURE_PCT = 0.02


def grams(s, lo=2, hi=4):
    """2~4 字的连续汉字片段 —— 本项目一直用的"主语"代理（不是分词器）。"""
    out = set()
    for m in CJK.finditer(s or ""):
        w = m.group(0)
        for n in range(lo, hi + 1):
            for i in range(len(w) - n + 1):
                out.add(w[i:i + n])
    return out


def furniture(titles, pct=FURNITURE_PCT):
    """全局标题词频超过 pct 的 n-gram 当家具。**按分布定**，不手写。"""
    df = Counter()
    for t in titles:
        for g in grams(t):
            df[g] += 1
    n = max(len(titles), 1)
    return {g for g, c in df.items() if c > n * pct}


def load_topic_titles(day_cut=None):
    """→ {tid: [(day, label, title)]}；`day_cut` 给了就只取 `fetched_at <= day_cut` 的成员。"""
    sql = """
        SELECT t.id, coalesce(t.label,''), coalesce(i.title,''),
               to_char(i.fetched_at, 'MM-DD')
        FROM feed_topics t
        JOIN feed_item_topics it ON it.topic_id = t.id
        JOIN feed_items i ON i.id = it.item_id"""
    if day_cut:
        sql += f" WHERE to_char(i.fetched_at, 'MM-DD') <= '{day_cut}'"
    out = defaultdict(list)
    for r in corpus.psql_rows(sql):
        if len(r) >= 4 and r[0]:
            out[int(r[0])].append((r[3], r[1], r[2]))
    return out


def multi_grams(by_topic, day_cut=None, furn=None):
    """→ {tid: 该议题成员标题的词集合}，只保留 **≥2 条成员**的议题。

    单条议题不参与：它们的"过半"就是它自己 ⇒ 签名退化成标题里的词，没有意义。
    """
    flat = {}
    for tid, mem in by_topic.items():
        upto = [t for d, _, t in mem if day_cut is None or d <= day_cut]
        if len(upto) >= 2:
            g = grams(" ".join(upto))
            flat[tid] = (g - furn) if furn else g
    return flat


def entry_words(flat, lo=LO, hi=HI):
    """入口词 = 覆盖 lo~hi 个**议题**的词（按议题计数，不按条目）。"""
    cover = Counter()
    for tg in flat.values():
        for g in tg:
            if len(g) >= 3:
                cover[g] += 1
    return {g for g, c in cover.items() if lo <= c <= hi}


def hang(flat, words, topk=TOPK):
    """把议题挂到入口上 —— **每个议题最多挂 topk 个**（封顶 = 不串）。

    挂哪几个：命中词里**最长的优先**（更长的字面更具体），并去掉被更长片段包含的短片段。
    """
    out = defaultdict(list)
    for tid, tg in flat.items():
        hit = sorted((g for g in tg if g in words), key=len, reverse=True)
        picked = []
        for g in hit:
            if not any(g in k for k in picked):
                picked.append(g)
            if len(picked) >= topk:
                break
        for g in picked:
            out[g].append(tid)
    return out


def measure(lo=LO, hi=HI, topk=TOPK, day_cut=None):
    """一次完整读数 —— 回放探针与每日曲线用的是**同一个函数**（免得两处各算各的）。"""
    by_topic = load_topic_titles(day_cut)
    titles = [t for mem in by_topic.values() for _, _, t in mem]
    furn = furniture(titles)
    flat = multi_grams(by_topic, day_cut, furn)
    words = entry_words(flat, lo, hi)
    h = hang(flat, words, topk)
    covered = len({tid for tids in h.values() for tid in tids})
    return {
        "items": len(titles),
        "topicsAll": len(by_topic),
        "topicsMulti": len(flat),
        "entries": len(h),
        "biggest": max((len(v) for v in h.values()), default=0),
        "covered": covered,
        "coverage": covered / max(len(flat), 1),
        "compression": len(flat) / max(len(h), 1),
        "entryWords": sorted(h),
        "lo": lo, "hi": hi, "topk": topk,
    }
