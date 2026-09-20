# -*- coding: utf-8 -*-
"""
**检索层** —— 声明式"臂"。一个臂 = 取哪些查询 × 每条取几个 × 怎么融 × 装几个 × 什么过滤。

现有检索器：
  · `vec`  —— 复刻：向量检索 + RRF 融合（**只有向量一条通道**）
  · `live` —— 真身：走应用的 `/api/chat`，读 `done` 事件的 sources（**无偏但高噪声，每臂要 ≥3 次**）

`vec` 的旋钮（历史上逐个试过的都在这里，以后不用再写新脚本）：
  k          每条查询取几个            8 / 12 / 16 / 32 / 64
  cap        融合后装几个（名额≈max-contexts）  12 / 24 / 48
  kind       索引文本                  ctx+body / body / ctx
  thresh     距离门槛（1-cos），None=不设     0.60 / 0.70 …
  mmr        冗余过滤 (kind, 阈值)，None=不过滤   ("cos",0.90) / ("lit",0.45)
  queries    用哪批查询：`planner`（真实规划器产出的，默认）或 `raw`（原始问句）
"""
from . import corpus

PLANNER = "planner"
RAW = "raw"


def _queries(C, cs, rec, source):
    """一律返回**列表的列表**（每题一组查询）。返回字符串列表会被外面按字符遍历（踩过）。"""
    if source == RAW:
        return [[c["q"]] for c in cs.cases]
    if source == PLANNER and rec and "queries" in rec[0]:
        return [(r.get("queries") or [r["q"]]) for r in rec]
    return [[c["q"]] for c in cs.cases]


def _bigrams(s):
    import re
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def vec(C, cs, k=8, cap=24, kind="ctx+body", thresh=None, mmr=None,
        query_source=PLANNER, rec=None, want_pool=False):
    """向量 + RRF。返回 (每题入选集合, 每题候选池集合, 每题**有序**入选列表)。

    有序列表也返回，是因为有些判据看的是**名次**不只是"在不在"——
    自检索问的是「切块切得好不好」，**排第一才算好**（与多跳的"全中"是两回事）。
    """
    V = C.vecs(kind)
    qs = _queries(C, cs, rec, query_source)
    qv = C.qvec([q for group in qs for q in group])

    picked, pools, orders = [], [], []
    for group in qs:
        scored, dist = {}, {}
        for q in group:
            order = sorted(((corpus.cos(qv[q], v), j) for j, v in enumerate(V)),
                           reverse=True)[:k]
            for rank, (c, j) in enumerate(order):
                scored[j] = scored.get(j, 0) + 1 / (60 + rank)
                dist[j] = min(dist.get(j, 9), 1 - c)
        order = [j for j, _ in sorted(scored.items(), key=lambda x: -x[1])]
        if thresh is not None:
            order = [j for j in order if dist[j] <= thresh]
        if mmr is not None:
            mkind, mthr = mmr
            keep = []
            for j in order:
                dup = False
                for i in keep:
                    if mkind == "cos":
                        if corpus.cos(V[j], V[i]) >= mthr:
                            dup = True
                            break
                    else:
                        a, b = _bigrams(C.body[j]), _bigrams(C.body[i])
                        if a and b and len(a & b) / len(a | b) >= mthr:
                            dup = True
                            break
                if not dup:
                    keep.append(j)
            order = keep
        pools.append(set(order))
        orders.append(order[:cap])
        picked.append(set(order[:cap]))
    return picked, pools, orders
