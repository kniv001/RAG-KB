# -*- coding: utf-8 -*-
"""**句级 kNN 边**：换成"靶句"判据 + 加边过滤之后，一跳还剩多少天花板？

## 为什么单独一个探针（而不是改上一个）

`graph-bridge-probe.py --unit sent` 报的是"靶**块**进没进" —— **判据太粗**：
块进来了不等于**答案要的那一句**进来了。这一版换成**靶句**（含摘录的那句），
并加上真做时绕不过去的**边过滤**：

  1. **相似度地板**：邻居与源句的余弦低于地板就不带（泛泛而"像"的句子不要）
  2. **只留带来新块的邻居**：同一块里再来十句，对"桥接"没有意义，只烧预算

## 口径

- 块召回 = 生产口径（`ctx+body`、top-8）
- 选中句 = 这些块里按问题相似度取 top-20（生产口径 `sent-window-m`）
- 一跳 = 每个选中句的 top-N 邻句（句-句余弦，分块算）→ 过滤 → 计入
- 判据 = **靶句进没进**（靶句 = 规范化后包含摘录的句子，与 `sent-recall.py` 同一套解析）

用法：python tools/graph-bridge-sent-probe.py [题集…] --hop 3,10 --floor 0.0,0.5
      python tools/graph-bridge-sent-probe.py multihop-127 --perblock 3,5,8

## 实测（2026-09-26，multihop-127，k=8，判据=**靶句**）

| 选句结构 | 靶句命中 | 注入句 |
|---|---|---|
| 基线（**全局共享 top-20 窗口** —— 生产口径） | 107/127 | 20 |
| 块级图 +10 邻块 | 124/127 | 28 **块** ≈ 13k 字 |
| 句级图 +3 邻句 | 110/127 | 50 |
| 句级图 +10 邻句 | 114/127 | 88 |
| **逐块 5 句**（`KB_SENT_CHUNK_K=5`） | **113/127** | **39** |
| **逐块 8 句** | **117/127** | 57 |

**结论一：图不是这里该动的杠杆。** 同一份增益，"逐块配额"用一半的句数就拿到
（113@39 句 vs 114@88 句），`--perblock 8` 还反超（117）。

**结论二：瓶颈在"共享窗口"，不在召回** —— 把 k 从 8 加到 24，命中反而**一路下滑**
（107→103→99→97）：选句是"召回块里共享的 top-M"，块越多候选越多，靶句越挤不进去。
逐块配额直接治这个稀释。

⚠️ **`KB_SENT_CHUNK_K` 项目里早就有，而且当初量过"测不出差别 ⇒ 不转正"**
（2026-09-23，21 题 × 2 遍，判据是装入 token 与质量）。**这次测出来的是第三件事 ——
召回**（靶句命中），而且语料已经换了（661 → 877 块、avg 9 → 10 句/块）。
两次不矛盾：**瓶颈会搬家**，当初的判据量不到今天这个损失。
⇒ 该做的不是建图，是**在今天的语料上重测那个已有开关**（判据换成靶句命中 + 预算）。

⚠️ **另两处水分**（自己先前报过的数要打折）：
· `graph-bridge-probe.py --unit sent` 报的 "127/127 全中" 用的是**靶块**判据（粗）；
  换成靶句只有 **114**。块进来了 ≠ 答案要的那句进来了。
· 相似度地板（0.45 / 0.6）**完全不起作用** —— top-10 邻句全在 0.6 以上，
  余弦在句级几乎是"稠密图"，没有区分力。这正是"松判据不区分"的又一例。
"""
import hashlib
import importlib.util
import io
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


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    arg = lambda k, d: (sys.argv[sys.argv.index(f"--{k}") + 1]        # noqa: E731
                        if f"--{k}" in sys.argv else d)
    _opts = {"--k", "--hop", "--floor", "--m"}
    names = [a for i, a in enumerate(sys.argv[1:])
             if not a.startswith("--") and sys.argv[1:][i - 1] not in _opts] or \
        ["multihop-127", "xdoc-8"]
    ks = [int(x) for x in arg("k", "8").split(",")]
    M = int(arg("m", "20"))
    hops = [int(x) for x in arg("hop", "3,10").split(",")]
    floors = [float(x) for x in arg("floor", "0.0,0.45,0.6").split(",")]
    # **逐块配额**：每块各出自己最相关的 q 句（而不是全局共享一个 M 窗口）
    quotas = [int(x) for x in arg("perblock", "").split(",") if x.strip()]

    import sent_index
    sr = _load("sent-recall")

    C = corpus.load()
    V = np.array(C.vecs("ctx+body"), dtype=np.float32)
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    id_of = {i: int(C.ids[i]) for i in range(C.n)}
    pos_of = {int(c): i for i, c in enumerate(C.ids)}

    keyed = sent_index._load_cache().get("keyed") or {}
    rows = sent_index.load_rows()                       # (chunk_id, seq, sent_hash, text)
    keep, chunk_of, S6 = [], [], []
    for cid, seq, sh, txt in rows:
        v = keyed.get(hashlib.sha1(txt.encode("utf-8")).hexdigest()[:16])
        if v is None:
            continue
        keep.append(v)
        chunk_of.append(int(cid))
        S6.append((None, cid, seq, txt, sh, None))      # sent-recall 的列序
    SV = np.array(keep, dtype=np.float32)
    SV /= (np.linalg.norm(SV, axis=1, keepdims=True) + 1e-9)
    chunk_of = np.array(chunk_of)
    print(f"{C.line()}　句向量 {SV.shape[0]} 句 / {len(set(chunk_of.tolist()))} 块")

    # 邻句表（带相似度，方便按地板过滤）
    NB = {}
    for h in hops:
        idx = np.zeros((SV.shape[0], h), dtype=np.int32)
        sim = np.zeros((SV.shape[0], h), dtype=np.float32)
        for i in range(0, SV.shape[0], 512):
            blk = SV[i:i + 512] @ SV.T
            blk[np.arange(blk.shape[0]), np.arange(i, i + blk.shape[0])] = -1
            part = np.argpartition(-blk, h, axis=1)[:, :h]
            idx[i:i + 512] = part
            sim[i:i + 512] = np.take_along_axis(blk, part, axis=1)
        NB[h] = (idx, sim)
    print(f"邻句表已算好（{'/'.join(str(h) for h in hops)}）")

    for name in names:
        cs = cases_mod.load(name, C)
        tgt, unresolved = sr.target_sentences(C, cs, S6)
        QV = C.qvec([t[1] for t in tgt])
        print(f"\n── {name}（{len(tgt)} 题，靶句解析不到 {len(unresolved)} 道）")
        for k in ks:
            # **基线**：不加任何跳，只用现有选中句 —— 没有这一行就没法判"跳有没有用"
            b_ok, b_inj = 0, []
            for cid, q, groups in tgt:
                qv = np.array(QV[q], dtype=np.float32)
                pick = [int(i) for i in np.argsort(-(V @ qv))[:k]]
                mine = np.where(np.isin(chunk_of, [id_of[p] for p in pick]))[0]
                sel = set(int(i) for i in mine[np.argsort(-(SV[mine] @ qv))[:M]])
                b_ok += bool(set().union(*groups) & sel) if groups else False
                b_inj.append(len(sel))
            print(f"  k={k:<3}{'基线':>5}{b_ok:>7}/{len(tgt):<4}{int(np.median(b_inj)):>6} 句")
            for q in quotas:      # **逐块配额**：同样的块，换个选句结构
                ok, inj = 0, []
                for cid, qq, groups in tgt:
                    qv = np.array(QV[qq], dtype=np.float32)
                    pick = [int(i) for i in np.argsort(-(V @ qv))[:k]]
                    sel = set()
                    for p in pick:
                        idx = np.where(chunk_of == id_of[p])[0]
                        sel |= set(int(i) for i in idx[np.argsort(-(SV[idx] @ qv))[:q]])
                    ok += bool(set().union(*groups) & sel) if groups else False
                    inj.append(len(sel))
                print(f"      逐块{q}句：{ok:>3}/{len(tgt):<4}{int(np.median(inj)):>6} 句")
            for fl in floors:
                for h in hops:
                    ok = inj_tot = 0
                    inj = []
                    for cid, q, groups in tgt:
                        qv = np.array(QV[q], dtype=np.float32)
                        pick = [int(i) for i in np.argsort(-(V @ qv))[:k]]
                        mine = np.where(np.isin(chunk_of, [id_of[p] for p in pick]))[0]
                        sel = set(int(i) for i in mine[np.argsort(-(SV[mine] @ qv))[:M]])
                        xi, xs = NB[h]
                        add = set()
                        have_blocks = {int(chunk_of[i]) for i in sel}
                        for i in sel:
                            for j, s in zip(xi[i], xs[i]):
                                j = int(j)
                                if s < fl:
                                    continue
                                b = int(chunk_of[j])
                                if b in have_blocks and j not in sel:
                                    continue      # 不带来新块的邻居：不带（只烧预算）
                                add.add(j)
                        ok += bool(set().union(*groups) & (sel | add)) if groups else False
                        inj.append(len(sel) + len(add))
                    print(f"      地板{fl} +{h}跳：{ok:>3}/{len(tgt):<4}"
                          f"{int(np.median(inj)):>6} 句")
    print("\n参照：现在的分层注入 = 选中句 M=20（装入 token 中位 2327，约 1000 字）")


if __name__ == "__main__":
    main()
