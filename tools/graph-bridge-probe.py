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


def main():
    arg = lambda k, d: (sys.argv[sys.argv.index(f"--{k}") + 1]        # noqa: E731
                        if f"--{k}" in sys.argv else d)
    names = [a for a in sys.argv[1:] if not a.startswith("--")] or \
        ["multihop-127", "xdoc-8", "hard"]
    ks = [int(x) for x in arg("k", "8").split(",")]
    hops = [int(x) for x in arg("hop", "3,10").split(",")]

    C = corpus.load()
    V = np.array(C.vecs("ctx+body"), dtype=np.float32)            # (n, 1024)
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    S = V @ V.T                                                   # 块-块相似度
    np.fill_diagonal(S, -1)
    print(C.line() + f"　块-块相似度矩阵 {S.shape} 已算好")

    # 每块的最相似邻块（一次算好，后面复用）
    NB = {n: np.argpartition(-S, n, axis=1)[:, :n] for n in hops}

    for name in names:
        cs = cases_mod.load(name, C)
        targets = [(c["id"], c["q"], c["_targets"]) for c in cs.cases]
        QV = C.qvec([t[1] for t in targets])
        order = {t[0]: np.argsort(-(V @ np.array(QV[t[1]], dtype=np.float32)))
                 for t in targets}
        print(f"\n── {name}（{len(targets)} 题）")
        head = f"{'k':>4}" + "".join(f"{'命中':>8}{'+' + str(h) + '跳':>9}"
                                     f"{'注入块':>9}" for h in hops)
        print(head)
        for k in ks:
            line = f"{k:>4}"
            for h in hops:
                base = exp = 0
                injected = []
                for cid, q, gs in targets:
                    groups = [t.group for t in gs]
                    pick = set(int(i) for i in order[cid][:k])
                    b = allhit(groups, pick)
                    base += b
                    grow = set(pick)
                    for i in pick:
                        grow |= set(int(x) for x in NB[h][i])
                    e = allhit(groups, grow)
                    exp += e
                    injected.append(len(grow))
                line += f"{base:>8}{exp:>9}{int(np.median(injected)):>9}"
            print(line)
        print("  （命中 = 全部靶子组都进 top-k；+N跳 = 命中块各自带 N 个邻块之后）")


if __name__ == "__main__":
    main()
