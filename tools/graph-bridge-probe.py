# -*- coding: utf-8 -*-
"""**块-块图（短索引）值不值得建** —— 量"一跳扩展"的天花板。

## 这个探针回答什么（用户的提法，2026-09-26）

> 在底层的知识块/句子块之间建立分布式的图连接，通过关联度桥接 ——
> 一个**长索引**（议题 → 某个准确的知识块）+ 多个**短索引**（块与块之间的关联分布）。

**长索引那半已经有了**：主题树（`tree_nodes`）+ 议题层（`feed_topics` 的质心与 7 天窗）
就是"从粗到某个块"的路由。**短索引那半才是新东西**：块与块之间的边。

而块-块图**只在一种情形下有用**：检索**已经落在靶子附近**，只是没落准 ——
图能把邻块桥过来。如果检索命中的块**根本不在靶子附近**，图再怎么连也接不上
（它不知道靶子在哪）。所以先量这个**先决条件**：

    现在答不出来的题，一跳之内够得着靶子吗？

## 判据与口径

- 检索口径 = 生产口径（`ctx+body`、top-k = 8）
- 靶子 = 题集里解析出来的块组（`--all-hit`：**每组都要有块进 top-k**）
- 一跳 = 命中块各自的最相似 N 个邻块（块-块余弦，全库 877×877 一次性算）
- 同时报**代价**：扩展后要注入多少块（≈ 每块 480 字 —— 项目对上下文预算是敏感的）

用法：python tools/graph-bridge-probe.py [题集…]（默认 multihop-127 xdoc-8 hard）
      python tools/graph-bridge-probe.py --k 8,16 --hop 3,10
      python tools/graph-bridge-probe.py multihop-127 --unit sent   # 句级（推荐看这个）

## 实测（2026-09-26，top-8，`ctx+body`，语料 877 块 / 8639 句）

| 题集 | 命中@8 | **块级 +10 邻块** | 注入 | **句级 +3 邻句** | 注入 | **句级 +10 邻句** | 注入 |
|---|---|---|---|---|---|---|---|
| multihop-127 | 107/127 | 124（+17） | 28 块 ≈ 13k 字 | **119（+12）** | 56 句 ≈ 2.8k 字 | **127（+20，全中）** | 98 句 ≈ 4.9k 字 |
| xdoc-8 | 6/8 | 8/8（+2） | 24 块 | **8/8（+2）** | 53 句 | 8/8 | 94 句 |
| hard | 10/10 | 10/10 | — | — | — | — | — |

**结论：图建在句子上**（同一份桥接能力，代价差一个数量级）。
生产口径的参照：分层注入现在是 20 句 ≈ 1000 字（装入 token 中位 2327）
⇒ `+3 邻句` 是可行的操作点（+1800 字 ≈ +1000 token 换 12 道题），`+10 邻句` 太贵。

⚠️ **两处要把话说清楚**（免得把天花板当承诺）：
1. 命中判据是**靶块**进没进（粗），不是"答案要的那句进了" —— 真做之前要换成靶句。
2. 这里的边是**纯余弦**。今天另有一处实测说明了"松判据不区分"：判类换成
   「有关、能提供部分信息」之后，该判丙的 5 道全被推成乙。句-句边很可能同样
   把"像但没用"的句子拖进来 ⇒ **98 句是代价上界**，真做必须先加过滤（相似度地板／
   只留能带来新块的邻居）。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ruler import corpus, cases as cases_mod                    # noqa: E402


def allhit(groups, pick):
    """`--all-hit`：**每一组**靶子都要有块进了 pick。"""
    return all(any(j in pick for j in g) for g in groups)


def sent_level(hops):
    """**句级**：块召回 → 块内挑句（生产口径 M=20）→ 一跳扩句 → 看能不能到靶块。

    为什么量这一版：块级扩边**代价不可行**（+10 邻块 = 28 块 ≈ 13k 字，
    而分层注入刚把装入压到 2300 token）。句子层已经有向量与地址，
    一跳只要 +N 句 ≈ 几十到一两百字 —— 同样的桥接能力能不能保住，看这里。
    """
    import sent_index
    cache = sent_index._load_cache().get("keyed") or {}
    rows = sent_index.load_rows()                    # (chunk_id, seq, sent_hash, text)
    import hashlib
    keep, chunk_of = [], []
    for cid, seq, sh, txt in rows:
        v = cache.get(hashlib.sha1(txt.encode("utf-8")).hexdigest()[:16])
        if v is None:
            continue
        keep.append(v)
        chunk_of.append(int(cid))
    SV = np.array(keep, dtype=np.float32)
    SV /= (np.linalg.norm(SV, axis=1, keepdims=True) + 1e-9)
    chunk_of = np.array(chunk_of)
    print(f"句向量 {SV.shape[0]} 句（覆盖 {len(set(chunk_of.tolist()))} 块）")
    NB = {}
    for h in hops:                                   # **分块算**：别一次开 8639²（74M 个数）
        best = np.zeros((SV.shape[0], h), dtype=np.int32)
        for i in range(0, SV.shape[0], 512):
            blk = SV[i:i + 512] @ SV.T
            blk[np.arange(blk.shape[0]), np.arange(i, i + blk.shape[0])] = -1   # 自己不算邻居
            best[i:i + 512] = np.argpartition(-blk, h, axis=1)[:, :h]
        NB[h] = best
    print(f"邻句表已算好（{'/'.join(str(h) for h in hops)}）")
    return SV, chunk_of, NB


def main():
    arg = lambda k, d: (sys.argv[sys.argv.index(f"--{k}") + 1]        # noqa: E731
                        if f"--{k}" in sys.argv else d)
    # **选项的值不算题集名**（第一版忘了，于是 `--unit sent` 里的 `sent` 被当成题集去加载）
    _opts = {"--k", "--hop", "--unit"}
    names = [a for i, a in enumerate(sys.argv[1:])
             if not a.startswith("--") and sys.argv[1:][i - 1] not in _opts] or \
        ["multihop-127", "xdoc-8", "hard"]
    ks = [int(x) for x in arg("k", "8").split(",")]
    hops = [int(x) for x in arg("hop", "3,10").split(",")]
    unit = arg("unit", "block")

    C = corpus.load()
    V = np.array(C.vecs("ctx+body"), dtype=np.float32)            # (n, 1024)
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    S = V @ V.T                                                   # 块-块相似度
    np.fill_diagonal(S, -1)
    print(C.line() + f"　块-块相似度矩阵 {S.shape} 已算好")

    # 每块的最相似邻块（一次算好，后面复用）
    NB = {n: np.argpartition(-S, n, axis=1)[:, :n] for n in hops}
    # **下标 ↔ 块 id 两张表**：题集里的靶子是**下标**，句子表里存的是**块 id**
    id_of = {i: int(C.ids[i]) for i in range(C.n)}
    pos_of = {int(c): i for i, c in enumerate(C.ids)}

    if unit == "sent":
        SV, chunk_of, SNB = sent_level(hops)
        print(f"（句级模式：从块召回的句子里扩 {hops} 句，再看靶块进没进）")

    for name in names:
        cs = cases_mod.load(name, C)
        targets = [(c["id"], c["q"], c["_targets"]) for c in cs.cases]
        QV = C.qvec([t[1] for t in targets])
        order = {t[0]: np.argsort(-(V @ np.array(QV[t[1]], dtype=np.float32)))
                 for t in targets}
        print(f"\n── {name}（{len(targets)} 题）　模式={unit}")
        print(f"{'k':>4}" + "".join(f"{'命中':>8}{'+' + str(h) + '跳':>9}"
                                    f"{'注入':>9}" for h in hops))
        for k in ks:
            line = f"{k:>4}"
            for h in hops:
                base = exp = 0
                injected = []
                for cid, q, gs in targets:
                    groups = [t.group for t in gs]
                    pick = set(int(i) for i in order[cid][:k])
                    base += allhit(groups, pick)
                    if unit == "block":
                        grow = set(pick)
                        for i in pick:
                            grow |= set(int(x) for x in NB[h][i])
                        n_inj = len(grow)
                    else:
                        # 块召回 → 这些块里的句子按问题取 top-M（生产口径）→ 扩 h 句
                        # ⚠️ **位置 ≠ 块 id**：`pick` 是语料里的**下标**，而 `chunk_of` 是**块 id**。
                        # 第一版直接拿两者比 ⇒ `mine` 全空 ⇒ exp 恒为 0（一个"看起来是负结果"的 bug）。
                        want = [id_of[p] for p in pick]
                        idx = np.where(np.isin(chunk_of, want))[0]
                        qv = np.array(QV[q], dtype=np.float32)
                        sims = SV[idx] @ qv
                        sel = idx[np.argsort(-sims)[:20]]
                        nb = SNB[h][sel].reshape(-1)
                        grow = {pos_of[c] for c in chunk_of[sel].tolist() if c in pos_of}
                        grow |= {pos_of[c] for c in chunk_of[nb].tolist() if c in pos_of}
                        n_inj = len(sel) + len(set(nb.tolist()))
                    exp += allhit(groups, grow)
                    injected.append(n_inj)
                line += f"{base:>8}{exp:>9}{int(np.median(injected)):>9}"
            print(line)
        print("  （命中 = 全部靶子组都进 top-k；+N跳 = 命中之后各自带 N 个邻居；"
              "块级注入单位是块，句级是句）")


if __name__ == "__main__":
    main()
