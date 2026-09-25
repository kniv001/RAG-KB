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

## ⭐ 结构边：真正的答案（用户 2026-09-26 追问"换成语义相似度行不行"）

**先澄清**：这里的边**本来就是语义相似度**（bge-m3 的 embedding 余弦），不是字面相似。
所以"换成语义"没得换 —— 真正的分野是 **"相似度边（稠密、没区分力）" vs "结构/类型边（稀疏、有类型）"**。

四种边都量了（选句结构固定为"逐块 q 句"，判据=靶句，multihop-127，k=8）：

| 配置 | 靶句命中 | 注入句 | 块数 |
|---|---|---|---|
| 基线（全局 top-20） | 107/127 | 20 | 8 |
| 逐块 5 句（只召回块） | 113 | 39 | 8 |
| 语义 kNN 图 +10 邻句 | 114 | 88 | — |
| **相邻块 seq±1 · 每块 5 句** | **120** | **72** | 16 |
| 同文档 · 每块 5 句 | 121 | 198 | 43 |
| 同簇（`tree_nodes`）· 每块 5 句 | **122** | **499** | 111 |
| **随机块**（对照）· 每块 5 句 | 113 | 113 | 24 |

三条读法：
1. **结构边 > 相似度边**：相邻块 120@72 完胜语义图 114@88（命中更高、代价更低）
2. **随机对照证明增益来自"边有信息"**，不是"加得多"：随机 24 块 / 113 句 ⇒ 命中一点没动
3. **边越宽越贵**：同簇最好（122）但拖进 111 块 / 499 句；每块句数 q 的曲线也很陡
   （相邻块：q=2 → 109@32、q=3 → 113@46、q=5 → 120@72）

⇒ **结论修正**：不是"建不建图"的问题，而是 **"加一条文档补全规则"** ——
召回命中某块时，把它 `seq±1` 的相邻块也带上（每块再取 top-5 句）。
它**不需要任何持久化的边表**（`seq±1` 就是一条 WHERE 条件），也不是分布式的东西。

## ⭐⭐ 跨文档那一半（用户追问"跨文档方面呢"，2026-09-26）

**先纠一个直觉**：语义 kNN 边**不等于**跨文档边 —— 实测 top-10 邻句里
**同文档 66%、跨文档只占 34%**。所以"用语义边就能跨文档"是错的，它大部分留在文档内。

库里能跨文档的边只有两条，量下来都不划算：

| 边 | multihop-127 | xdoc-8 | 注入句 | 块 |
|---|---|---|---|---|
| 主题簇（`tree_nodes`，**不封顶**） | **122** | — | **499** | 111 |
| 主题簇 + 封顶 3 块 | 114 | 7/8 | 52 | 11 |
| **事件议题**（`feed_topics`） | **113 ＝ 没涨** | 6/8 | 39 | **8（没扩展）** |
| **相邻块 + 封顶簇边（组合）** | **120**（＝只加相邻块） | 7/8 | 72 | 16 |

三条读法：
1. **组合 = 相邻块**：封顶的跨文档簇边**被相邻块完全吸收**（命中、句数、块数三样都一样）
   ⇒ 跨文档那条边在"封顶到能承受的规模"之后**一点额外贡献都没有**
2. **不封顶确实最准（122）但代价 499 句** —— 一个跨文档边把整个簇拖进来
3. **事件边在技术文档上完全不生效**：它只覆盖 **37/81** 篇（只有已晋升的新闻条目有议题边），
   而 multihop 那批题问的是技术文档 ⇒ **一条边都连不上**（块数还是 8）

**根因是粒度**：`tree_nodes` 只有 **11 个簇 / 877 块**（平均一簇 80 块）——
那么粗的边，要么太松（拖进整库），要么一封顶就什么都没剩下。
（建树时**按 LLM 给的标签合并了簇**，这是它粗的原因。）

⇒ **想让跨文档真的赚钱，得先把那条边做细**：按**文档级**聚类（或把簇数从 11 提到 30~50、
不做标签合并），再回来看"同主题不同文档"这条边值多少。
现在这个粒度下，跨文档**赚不到**。
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
    _opts = {"--k", "--hop", "--floor", "--m", "--perblock", "--edges", "--qref"}
    names = [a for i, a in enumerate(sys.argv[1:])
             if not a.startswith("--") and sys.argv[1:][i - 1] not in _opts] or \
        ["multihop-127", "xdoc-8"]
    ks = [int(x) for x in arg("k", "8").split(",")]
    M = int(arg("m", "20"))
    hops = [int(x) for x in arg("hop", "3,10").split(",")]
    floors = [float(x) for x in arg("floor", "0.0,0.45,0.6").split(",")]
    # **逐块配额**：每块各出自己最相关的 q 句（而不是全局共享一个 M 窗口）
    quotas = [int(x) for x in arg("perblock", "").split(",") if x.strip()]
    # **结构边**（有类型的边，而不是"像不像"）：doc=同文档；adj=相邻块；tree=同一主题簇
    edges = [x for x in arg("edges", "").split(",") if x.strip()]
    q_ref = int(arg("qref", "5"))          # 结构边那一档统一用逐块 5 句（今天量出的较优结构）

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

    # 结构边要用的两张表：块 id → 簇 id（主题树），簇 id → 块 id 列表
    block_cluster, cluster_chunks = {}, {}
    if "tree" in edges:
        for tid, ids in corpus.psql_rows("SELECT id, chunk_ids FROM tree_nodes"):
            members = [int(c) for c in (ids or "").strip("{}").split(",") if c.strip()]
            cluster_chunks[tid] = members
            for c in members:
                block_cluster[c] = tid
    doc_pos = {}                                  # 文档 → 该文档的块位置
    for i in range(C.n):
        doc_pos.setdefault(C.doc[i], []).append(i)

    def reached_blocks(pick, kind):
        """从召回块出发，**按边的类型**一跳能到达的块（位置集合，含召回自己）。

        · `doc`  —— 同文档的全部块（结构边：这份文档一起被召回）
        · `adj`  —— 同文档里 seq 相差 ≤1 的块（结构边：上下文连续）
        · `tree` —— 同一**主题簇**里的块（这是"长索引"那半边：议题 → 块）
        · `rand` —— 对照组：同样多但**随机**的块（用来判"是不是加块就有用"）
        """
        out = set(pick)
        if kind == "doc":
            for i in pick:
                out |= set(doc_pos.get(C.doc[i], []))
        elif kind == "adj":
            for i in pick:
                for j in doc_pos.get(C.doc[i], []):
                    if abs(int(C.seq[j]) - int(C.seq[i])) <= 1:
                        out.add(j)
        elif kind == "tree":
            for i in pick:
                tid = block_cluster.get(int(C.ids[i]))
                for b in cluster_chunks.get(tid, []):
                    if b in pos_of:
                        out.add(pos_of[b])
        elif kind == "rand":
            need = max(0, 3 * len(pick) - len(out))
            pool = np.setdiff1d(np.arange(C.n), np.fromiter(out, dtype=int),
                                assume_unique=False)
            out |= set(int(x) for x in np.random.default_rng(0).choice(
                pool, size=min(need, len(pool)), replace=False))
        return out

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
            # **结构边**：选句结构固定在"逐块 q_ref 句"，只换"块从哪来"
            for kind in edges:
                ok, inj, nblk = 0, [], []
                for cid, qq, groups in tgt:
                    qv = np.array(QV[qq], dtype=np.float32)
                    pick = [int(i) for i in np.argsort(-(V @ qv))[:k]]
                    blocks = reached_blocks(pick, kind)
                    sel = set()
                    for p in blocks:
                        idx = np.where(chunk_of == id_of[p])[0]
                        if not len(idx):
                            continue
                        sel |= set(int(i) for i in idx[np.argsort(-(SV[idx] @ qv))[:q_ref]])
                    ok += bool(set().union(*groups) & sel) if groups else False
                    inj.append(len(sel))
                    nblk.append(len(blocks))
                print(f"      边【{kind}】：{ok:>3}/{len(tgt):<4}{int(np.median(inj)):>6} 句"
                      f"　（块 {int(np.median(nblk))} 个）")
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
