# -*- coding: utf-8 -*-
"""
**句子召回尺子** —— 独立的一把，**不碰块召回那条路**。

## 它回答什么

块召回（`ruler.py` 那一套）量的是"靶子块进没进前 k 块"。这把尺子把**检索单位换成句子**
（6190 句 vs 661 块），问三件事：

1. **句子召回能不能召到靶子那句**（@k 全中率）
2. **同样注入预算下，句子臂和块臂谁强** —— 这是唯一有决定意义的比法：
   比"k 个"是不公平的（一句话只有 53 字，一块有 480 字），
   要比就比**注入同样多的字**，看谁能把靶子装进去
3. **互补性**：块臂漏掉的题，句子臂捞得回来吗（反之亦然）

## 靶子怎么锚（沿用本项目的纪律：**锚内容不锚位置**）

题目集里的靶子是 40 字摘录（`corpus.EXCERPT`）。靶子句 = **规范化后包含该摘录的句子**。
摘录可能跨句（块头正好落在句子边界上）⇒ **解析不到的题单独报出来，按"题废"处理而不是按 0 分**
（这是 `cases.py` 那条审计纪律的原样照搬）。

## 为什么缓存要按内容锚

向量缓存按位置对齐的话，语料一重建就**静默错位**（实测过：661 条里 438 条错、不报错、
只让所有指标一起变低）。所以 `_sentence_vecs.json` 按**嵌进去的那串字的哈希**寻址
（`sent_index.embed_and_cache` 里唯一一份实现，本脚本只是调用方）——
加文档只算新句子，而不是整份作废重算。

用法：
    python tools/sent-recall.py multihop-25
    python tools/sent-recall.py multihop-25 --k 5,10,20,50,100 --chunk-k 12
"""
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ruler import corpus                                    # noqa: E402
from ruler import cases as cases_mod                        # noqa: E402
import sent_index                                           # noqa: E402

BUDGETS = (5, 10, 20, 50, 100, 200)


def load_sentences(C):
    """读句子表，并**逐块核对内容锚** —— 对不上就是陈旧，直接拒跑（不猜）。

    2026-09-25 之前核的是**全库语料戳**，那个戳"加一篇文档就变" ⇒ 语料一动整表就报陈旧，
    哪怕只有几块真的变了。现在核的是**每行所据的块内容锚**
    （`md5(chunks.content)[:12]`，与 SQL 侧同一个算法）—— 它只在该块正文变了时才变。
    """
    rows = corpus.psql_rows(
        "SELECT id, chunk_id, seq, text, sent_hash, chunk_hash FROM sentences ORDER BY chunk_id, seq")
    if not rows:
        raise SystemExit("sentences 表是空的 —— 先跑 python tools/build-sentences.py")
    want = {cid: hashlib.md5(b.encode("utf-8")).hexdigest()[:12]
            for cid, b in zip(C.ids, C.body)}
    seen = set()
    stale = {}
    for r in rows:
        seen.add(r[1])
        w = want.get(r[1])
        if w is None or r[5] != w:
            stale[r[1]] = stale.get(r[1], 0) + 1
    missing = [cid for cid in want if cid not in seen]
    if stale or missing:
        raise SystemExit(
            f"句子索引与语料对不上：**陈旧 {len(stale)} 块（{sum(stale.values())} 行）**、"
            f"**没有句子 {len(missing)} 块**\n  例：陈旧 {list(stale)[:3]}　缺 {missing[:3]}\n"
            f"  ⇒ 重建：python tools/build-sentences.py（增量，只重切这几块）")
    return rows


def vecs(C, S):
    """句子向量：委托 `sent_index`（**按内容锚**，只有一份实现）。"""
    return sent_index.embed_and_cache([s[3] for s in S])


def target_sentences(C, cs, S):
    """每题的**靶子句下标**：规范化后包含摘录的句子。

    返回 (每题一组靶子下标, 解析不到的题)。
    """
    nrm = [corpus.norm(s[3]) for s in S]
    # 句子按 chunk 归组，便于"只在该题的靶子块/文档内找"
    by_chunk = {}
    for i, s in enumerate(S):
        by_chunk.setdefault(s[1], []).append(i)
    cidx = {}
    for i in range(C.n):
        cidx.setdefault(C.doc[i], {})[str(C.seq[i])] = i

    out, unresolved = [], []
    for c in cs.cases:
        groups = []
        for t in c["_targets"]:
            key = corpus.norm(t.spec.get("excerpt") or "")
            if not key:
                continue
            hits = []
            # ① 先在靶子块里找（快且准）
            for j in t.group:
                for si in by_chunk.get(C.ids[j], []):
                    if key in nrm[si] or (nrm[si] and nrm[si] in key):
                        hits.append(si)
            # ② 靶子块里没有 ⇒ 全库找（摘录可能落在切块边界外）
            if not hits:
                hits = [i for i, x in enumerate(nrm) if key and (key in x or (x and x in key))]
            if hits:
                groups.append(set(hits))
        if len(groups) < len(c["_targets"]) or not groups:
            unresolved.append(c["id"])
        out.append((c["id"], c["q"], groups))
    return out, unresolved


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    name = sys.argv[1]
    arg = lambda k, d: (sys.argv[sys.argv.index(f"--{k}") + 1]      # noqa: E731
                        if f"--{k}" in sys.argv else d)
    chunk_k = int(arg("chunk-k", "12"))
    ks = [int(x) for x in arg("k", ",".join(map(str, BUDGETS))).split(",")]

    C = corpus.load()
    S = load_sentences(C)
    print(f"语料 {C.n} 块 · 戳 {C.stamp}　句子 {len(S)} 句"
          f"（均 {sum(len(s[3]) for s in S)/len(S):.0f} 字）")
    cs = cases_mod.load(name, C)
    if cs.problems:
        print(f"⚠ 题目集审计有问题 {len(cs.problems)} 条（前 3）：{cs.problems[:3]}")
    tgt, unresolved = target_sentences(C, cs, S)
    n_t = sum(len(g) for _, _, g in tgt)
    print(f"题目 {len(tgt)} 道 · 靶子组 {n_t} 组"
          f"　**摘录跨句/解析不到 {len(unresolved)} 道**"
          + (f"：{unresolved[:5]}" if unresolved else ""))
    if unresolved:
        print("  （这些题按**题废**处理，不按 0 分计入 —— 与 cases.py 的审计纪律一致）")

    V = vecs(C, S)
    qv = C.qvec([q for _, q, _ in tgt])
    # 句子按块归组（分层臂要用；建一次，别在循环里重复建）
    by_chunk = {}
    for j, s in enumerate(S):
        by_chunk.setdefault(s[1], []).append(j)

    # ── 句子臂：top-k 句 ────────────────────────────────────────────────
    order_all = []
    for _, q, _ in tgt:
        o = sorted(range(len(V)), key=lambda j: -corpus.cos(qv[q], V[j]))
        order_all.append(o)

    ok = set(tgt[i][0] for i in range(len(tgt)) if not tgt[i][2])
    print(f"\n{'顶层 k':>8}{'全中':>8}{'至少一组':>10}{'注入字数(中位)':>16}")
    res = {}
    for k in ks:
        full = part = 0
        lens = []
        for i, (cid, q, groups) in enumerate(tgt):
            if cid in ok or not groups:
                continue
            pick = set(order_all[i][:k])
            hit = [g for g in groups if g & pick]
            full += len(hit) == len(groups)
            part += len(hit) > 0
            lens.append(sum(len(S[j][3]) for j in pick))
        n = len(tgt) - len(ok)
        res[k] = full / max(1, n)
        print(f"{k:>8}{full:>5}/{n:<4}{part:>7}/{n:<4}{int(sorted(lens)[len(lens)//2]):>14}")

    # ── 同预算对照：块臂 top-k 块 vs 句子臂"装到同样字数" ────────────────
    Vc = C.vecs("ctx+body")
    corder = []
    for _, q, _ in tgt:
        corder.append(sorted(range(C.n), key=lambda j: -corpus.cos(qv[q], Vc[j])))
    cfull = 0
    clens = []
    for i, (cid, q, groups) in enumerate(tgt):
        if cid in ok or not groups:
            continue
        pick = set(corder[i][:chunk_k])
        # ⚠️ **必须是"每组都命中"**（all-hit），与句子臂同一判据。
        # 第一版这里把 `groups` 拍平成一个集合、判 `any(...)` —— 那是"**任意一组**命中"，
        # 比句子臂松得多，于是块臂报出 126/127 而项目自己的尺子（同一配置）是 113/127。
        # 两条臂用两个判据比出来的差，**是尺子造出来的差**。
        cfull += all(
            any(b in C.ids and C.ids.index(b) in pick for b in {S[j][1] for j in g})
            for g in groups)
        clens.append(sum(len(C.body[j]) for j in pick))
    n = len(tgt) - len(ok)
    cmed = int(sorted(clens)[len(clens)//2])
    print(f"\n同预算对照（块臂 top-{chunk_k} 块 = {cmed} 字/题）：")
    print(f"  块臂　　　{cfull:>5}/{n} = {100*cfull/max(1,n):.0f}%　每題 {cmed} 字")
    for k in ks:
        # 找出"注入字数刚好不超过块臂中位"的那个 k
        lens = []
        for i, (cid, q, groups) in enumerate(tgt):
            if cid in ok or not groups:
                continue
            lens.append(sum(len(S[j][3]) for j in order_all[i][:k]))
        if lens and sorted(lens)[len(lens)//2] <= cmed * 1.15:
            print(f"  句子臂 top-{k:<4}{int(res[k]*n):>5}/{n} = {100*res[k]:.0f}%"
                  f"　每题 {int(sorted(lens)[len(lens)//2])} 字　← **预算相当**")

    # ── 另一个口径：**块级命中**（"拣到的句子里有没有落在靶子块里的"）────
    #
    # 为什么必须有这一档：靶子句是「包含摘录的那一句」，而摘录是**块头 40 字** ——
    # 那是**切块的产物**，未必是这一块里真正能答问题的那句。所以"句子全中率低"
    # 有两种可能：① 句子召回不行 ② 靶子句本身选得不对。
    # 块级命中把这两个分开：它只问"拣回来的句子**落在不落在靶子块里**"。
    print("\n块级命中口径（拣到的句子里，有没有落在靶子块内的 —— 绕开「块头摘录」这个切块产物）：")
    print("  （这一档若高而上一档低 ⇒ 句子召回找得到地方，只是**没挑中那一句**；"
          "两档都低 ⇒ 是召回的锅）")
    for k in ks:
        n = len(tgt) - len(ok)
        full = 0
        for i, (cid, q, groups) in enumerate(tgt):
            if cid in ok or not groups:
                continue
            pick = set(order_all[i][:k])
            tb = {S[j][1] for g in groups for j in g}
            got = {S[j][1] for j in pick}
            if tb & got:
                full += 1
        print(f"  句子臂 top-{k:<4}{full:>5}/{n} = {100*full/max(1,n):.0f}%")

    # ── 对称的精度口径：**注入的字里，有多少落在靶子块内** ────────────
    #
    # 前两档各自有偏：全中档对句子臂**偏严**（靶子句是块头摘录、是切块产物），
    # 块级命中档对句子臂**偏松**（碰到靶子块的一句话 ≠ 带够那块的内容）。
    # 这一档两臂对称、且直接对应"我装进去的东西有多少是用得上的"：
    #   = 注入文本里落在靶子块内的字数 ÷ 注入总字数
    print("\n精度口径（注入的字里，落在靶子块内的比例 —— 两臂对称，越高越省）：")
    rows = []
    for k in ks:
        acc = tot = 0
        for i, (cid, q, groups) in enumerate(tgt):
            if cid in ok or not groups:
                continue
            tb = {S[j][1] for g in groups for j in g}
            for j in order_all[i][:k]:
                L = len(S[j][3])
                tot += L
                if S[j][1] in tb:
                    acc += L
        rows.append((k, 100 * acc / max(1, tot), tot // max(1, len(tgt) - len(ok))))
    for k, pct, per in rows:
        print(f"  句子臂 top-{k:<4}　{pct:>5.1f}%　（每题注入 {per} 字）")
    # 块臂同一口径（同一预算区间）
    for ck in (6, 12, 24):
        acc = tot = 0
        for i, (cid, q, groups) in enumerate(tgt):
            if cid in ok or not groups:
                continue
            tb = {S[j][1] for g in groups for j in g}
            for j in corder[i][:ck]:
                L = len(C.body[j])
                tot += L
                if C.ids[j] in tb:
                    acc += L
        print(f"  块臂　 top-{ck:<4}　{100*acc/max(1,tot):>5.1f}%　（每题注入 {tot//max(1,len(tgt)-len(ok))} 字）")

    # ── 分层臂：块召回 top-K → **只在那些块里**挑句 → 只注入那几句 ──────
    #
    # 为什么这条臂值得单独量：它赚的**不是** prefill（那个便宜，3795 tok/s）。
    # 它的第二条收益线是 **decode 本身会随上下文变短而变快**
    # （实测：装入 5.7k token 时 76.8 tok/s，13k 时只剩 46~50）。
    # 所以"少装资料"在这里**第一次**可能真的值钱 —— 前提是质量不掉。
    #
    # 判据要比三样：① 靶子块还触得到吗（stage1 是否白做）
    #              ② **靶子内容保留多少**（这是关键：碰到 ≠ 带够）
    #              ③ 同样预算下，比平铺句子臂强多少
    print("\n分层臂（块 top-K → 块内按问题重排句子 → 只注入 top-M 句）：")
    for K in (int(arg("stage1", "12")),):
        for M in ks:
            touch = retain = 0.0
            n = len(tgt) - len(ok)
            acc = tot = 0
            lens = []
            for i, (cid, q, groups) in enumerate(tgt):
                if cid in ok or not groups:
                    continue
                tb = {S[j][1] for g in groups for j in g}
                cand = [j for x in corder[i][:K] for j in by_chunk.get(C.ids[x], [])]
                cand.sort(key=lambda j: -corpus.cos(qv[q], V[j]))
                pick = set(cand[:M])
                got = {S[j][1] for j in pick}
                touch += bool(tb & got)
                # **内容保留**：靶子块里一共多少句，拣回来多少句
                all_tb = {j for b in tb for j in by_chunk.get(b, [])}
                if all_tb:
                    retain += len(pick & all_tb) / len(all_tb)
                for j in pick:
                    L = len(S[j][3])
                    tot += L
                    if S[j][1] in tb:
                        acc += L
                lens.append(sum(len(S[j][3]) for j in pick))
            med = int(sorted(lens)[len(lens)//2])
            print(f"  块top-{K} → 句top-{M:<4}触达 {int(touch):>4}/{n}"
                  f"　靶子内容保留 {100*retain/n:>5.1f}%"
                  f"　精度 {100*acc/max(1,tot):>5.1f}%　每题 {med:>5} 字")

    # ── 互补性 ─────────────────────────────────────────────────────────
    if len(ks):
        kk = ks[-1]
        both = only_s = only_c = neither = 0
        for i, (cid, q, groups) in enumerate(tgt):
            if cid in ok or not groups:
                continue
            sp = set(order_all[i][:kk])
            sh = any(g & sp for g in groups)
            cp = set(corder[i][:chunk_k])
            tb = {S[j][1] for g in groups for j in g}
            ch = any(b in C.ids and C.ids.index(b) in cp for b in tb)
            if sh and ch:
                both += 1
            elif sh:
                only_s += 1
            elif ch:
                only_c += 1
            else:
                neither += 1
        print(f"\n互补性（句子臂 top-{kk} vs 块臂 top-{chunk_k}，**至少中一组**）：")
        print(f"  都中 {both}　**只有句子臂中 {only_s}**　只有块臂中 {only_c}　都漏 {neither}")
    print(f"\n语料戳 {C.stamp} · 句子表 {len(S)} 句 · 题目集 {name}"
          "　⇒ 引用这个数时带上这三样")


if __name__ == "__main__":
    main()
