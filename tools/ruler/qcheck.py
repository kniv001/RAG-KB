# -*- coding: utf-8 -*-
"""
**出题后看一眼两个量** —— 但它**不是**区分度的预测器，别当预测器用。

## 它是什么／不是什么（两次假设都被自己的数据推翻，记在这里免得重走）

**假设一：问句↔靶子余弦低 ⇒ 够难 ⇒ 会翻。** ❌
  · 老题 0.640（★28%） · 我写的新题 0.711（★22%） · hard2 **0.638**（★**14%**）
  · hard2 余弦最低，区分度却最差 ⇒ **余弦不预测区分度**。
  · 原因：嵌入模型本来就吃改写 —— **用词换了，向量还是近的。**

**假设二：靶子里最难那个的名次落在 2~8 ⇒ 会翻。** ❌
  · 落在该区间的比例：老题 36%（★28%） vs hard2 **50%**（★**14%**）—— **反着的**。

⇒ **没找到能预测区分度的写前指标。** 真正算数的只有那条判据本身
（三档索引文本下会不会翻），而它**跑起来只要几十秒**（纯检索，不调模型）——
所以**也不需要代理指标**：写完直接跑校准，留下会翻的。

那两个量留着是因为**读起来有用**：余弦/名次都是**靶子好不好召回**的刻画，
能解释"这题为什么恒过"，只是不能预测"它会不会翻"。

用法：python tools/ruler/qcheck.py <规格名或题目集名>
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ruler import corpus  # noqa: E402

HARD = 0.64        # 老题的中位；低于它才算"够难"
VEC = "ctx+body"
# ⚠️ **下面这个区间不是预测器** —— 见模块开头：两个假设都被数据推翻了。
# 留着只是因为"靶子排在几名"能解释**为什么恒过**（名次 0 = 靶子就是首条命中），
# 但**不能**预测它会不会翻。真正算数的只有那条判据本身，而它跑起来只要几十秒。
BAND = (2, 8)      # 有希望的名次区间


def worst_rank(C, V, qv, groups):
    """靶子里**最难召回**的那个，排在全库第几（0 = 第一）。"""
    r = []
    for g in groups:
        best = max(corpus.cos(qv, V[j]) for j in g)
        r.append(sum(1 for v in V if corpus.cos(qv, v) > best))
    return max(r) if r else -1


def rows_from_spec(C, spec):
    """规格文件 → [(id, doc, 问句, [靶子块下标])]，**不建表**，直接算。"""
    index = {}
    for i in range(C.n):
        index.setdefault(C.doc[i], {})[str(C.seq[i])] = i
    out = []
    for item in spec["cases"]:
        if spec.get("multi"):
            d, n, seqs, q = item
        else:
            d, (n, seqs, q) = spec.get("doc", ""), item
        idxs = [index.get(d, {}).get(str(s)) for s in seqs]
        if any(i is None for i in idxs):
            print(f"  ⚠ {n}：有块号在 {d[:20]} 里找不到，跳过")
            continue
        out.append((f"{spec.get('prefix','?')}{n:02d}", d, q, idxs))
    return out


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法：python tools/ruler/qcheck.py <规格名或题目集名>")
    name = sys.argv[1]
    C = corpus.load()
    V = C.vecs(VEC)

    sp = os.path.join(TOOLS, "cases", "_spec", f"{name}.json")
    if os.path.exists(sp):
        rows = rows_from_spec(C, json.load(io.open(sp, encoding="utf-8")))
    else:
        p = os.path.join(TOOLS, "cases", f"{name}.json")
        cs = __import__("ruler.cases", fromlist=["x"]).load(name, C)
        rows = [(c["id"], c["doc"], c["q"], [t.group[0] for t in c["_targets"]])
                for c in cs.cases]

    print(f"{'题':<8}{'余弦':>6}{'靶子最难名次':>12}  判定　问句")
    cos_list, ranks, band = [], [], 0
    for cid, doc, q, idxs in rows:
        qv = C.qvec([q])[q]
        best = max(corpus.cos(qv, V[i]) for i in idxs)
        rank = sum(1 for v in V if corpus.cos(qv, v) > best)   # 靶子最好那个的名次
        wrank = worst_rank(C, V, qv, [idxs])
        inband = BAND[0] <= wrank <= BAND[1]
        band += inband
        cos_list.append(best)
        ranks.append(wrank)
        print(f"{cid:<8}{best:>6.3f}{wrank:>12}  {'★会翻' if inband else ' 难翻'}　{q[:32]}")
    n = len(rows)
    cos_list.sort()
    ranks.sort()
    print(f"\n{n} 题　余弦中位 {cos_list[n//2]:.3f}　靶子最难名次中位 {ranks[n//2]}"
          f"（范围 {ranks[0]}~{ranks[-1]}）")
    print(f"落在名次 {BAND[0]}~{BAND[1]} 区间的 {band}/{n} = {100*band/max(1,n):.0f}%"
          "（**仅供参考，不是预测**）")
    print("⚠️ 这两个量都**不预测区分度** —— 详见模块开头：余弦(假设一)与名次(假设二)都被数据推翻了。")
    print("   要判会不会翻，直接跑校准（三档 × k，纯检索几十秒），别用代理。")


if __name__ == "__main__":
    main()
