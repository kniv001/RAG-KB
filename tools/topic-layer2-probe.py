# -*- coding: utf-8 -*-
"""
**「议题之上还有一层吗」探针** —— 用户问「能不能再加一层大类」。

这一问有两种完全不同的实现，先量再答：
  (甲) **分类**：人工/模型给一个固定值域（时政/财经/体育…），每个议题挂一个
       ⇒ 值域不膨胀，但它**承载不了信息**（用户点的是「亚运会」，不是「体育」）
  (乙) **层级**：让议题自己再聚一层（事件 → 事件群）
       ⇒ 这一层**能承载信息**，而且因为它把 N 个并成 1 个，**天然低增长**
            —— 前提是"上一层真的存在"（题上确实有梯度）

本探针只回答一件事：**（乙）的那一层是不是真的存在**。判据是"最近邻议题"分布：
   · 每个议题离**最近的另一个议题**有多近？
   · 若绝大多数议题的最近邻都远（<0.7），说明议题层之上**没有**可聚的东西 —— 那
     "大类"只能是（甲），也就是一个**装饰性的标签**，不解决膨胀。
   · 若有一撮议题的最近邻很近（≥0.8），那一撮就是"同一件事被切开了" —— 有真层级。

顺带量**单链会链成什么样**（连通分量在几个阈值下的规模）—— 这是"低阈值并簇"的
已知失败模式，本项目在"公文腔并簇"那节量过一次代价，这里量的是同一件事的另一面。

用法：python tools/topic-layer2-probe.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402


def load():
    rows = corpus.psql_rows(
        "SELECT id, coalesce(label,'(未命名)'), item_n, embedding::text "
        "FROM feed_topics WHERE embedding IS NOT NULL")
    ids, labels, ns, vecs = [], [], [], []
    for r in rows:
        if len(r) < 4 or not r[3]:
            continue
        try:
            v = np.fromstring(r[3].strip("[]"), sep=",")
        except Exception:
            continue
        if v.size != 1024:
            continue
        ids.append(int(r[0]))
        labels.append(r[1])
        ns.append(int(r[2]))
        vecs.append(v)
    M = np.vstack(vecs)
    # 归一化 ⇒ 内积即余弦
    M /= np.maximum(np.linalg.norm(M, axis=1, keepdims=True), 1e-9)
    return np.array(ids), labels, np.array(ns), M


def bar(n, mx, width=44):
    return "█" * max(0, min(width, int(round(n * width / mx)) if mx else 0))


def main():
    ids, labels, ns, M = load()
    print("=" * 74)
    print(f"议题 {len(ids)} 个（有质心的）—— 两两余弦，共 {len(ids)*(len(ids)-1)//2} 对")
    print("=" * 74)

    S = M @ M.T
    np.fill_diagonal(S, -1.0)

    # ── 一、最近邻议题（排除自己）──────────────────────────────────────────
    nn = S.max(axis=1)
    print("\n一、**每个议题离「最近的另一个议题」有多近**")
    print("   （这一列就是「上一层存不存在」的直接读数）")
    edges = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01]
    hist, _ = np.histogram(nn, bins=edges)
    mx = hist.max() if hist.size else 1
    for i in range(len(hist)):
        lo, hi = edges[i], edges[i + 1]
        tag = ""
        if lo >= 0.80:
            tag = "  ← **阈值 0.80 之上：本来就该并**"
        print(f"   {lo:.2f}~{hi:.2f} {hist[i]:>5} {hist[i]/len(nn)*100:>5.1f}%  {bar(hist[i], mx)}{tag}")
    print(f"   最近邻中位 **{np.median(nn):.3f}** · 均值 {nn.mean():.3f} · ≥0.80 的 {int((nn>=0.8).sum())} 个"
          f"（{(nn>=0.8).mean()*100:.1f}%）")

    # ── 二、单链连通分量：低阈值并簇会链成什么样 ─────────────────────────
    print("\n二、**单链（连通分量）在各级阈值下并成几群** —— 低阈值的已知失败模式")
    print(f"   {'阈值':>6}{'群数':>7}{'最大群':>8}{'第二大':>8}{'被并掉的议题':>12}")
    for t in (0.60, 0.65, 0.70, 0.75, 0.80):
        adj = S >= t
        seen = np.zeros(len(ids), dtype=bool)
        comps = []
        for i in range(len(ids)):
            if seen[i]:
                continue
            stack, comp = [i], []
            seen[i] = True
            while stack:
                u = stack.pop()
                comp.append(u)
                for v in np.nonzero(adj[u])[0]:
                    if not seen[v]:
                        seen[v] = True
                        stack.append(int(v))
            comps.append(comp)
        sizes = sorted((len(c) for c in comps), reverse=True)
        merged = sum(s - 1 for s in sizes)
        print(f"   {t:>6.2f}{len(comps):>7}{sizes[0] if sizes else 0:>8}"
              f"{sizes[1] if len(sizes) > 1 else 0:>8}{merged:>12}")
    print("   ⚠️ 若「最大群」随阈值下降**暴涨**，就是单链在链式合并（一个中间议题把两片连起来）")
    print("      ⇒ 低阈值并簇要么并太狠（链成一坨）、要么并不动（真层级不在这一档）")

    # ── 三、看几个例子：最近邻 ≥0.75 的那些对 ────────────────────────────
    print("\n三、**最近邻 ≥0.75 的议题**（人眼核对这些到底是不是同一件事）")
    pairs = []
    ii, jj = np.nonzero(S >= 0.75)
    for a, b in zip(ii, jj):
        if a < b:
            pairs.append((S[a, b], a, b))
    pairs.sort(reverse=True)
    seen = set()
    shown = 0
    for s, a, b in pairs:
        if a in seen or b in seen:
            continue
        seen.add(a)
        seen.add(b)
        print(f"   {s:.3f}  [{ns[a]:>3}] {labels[a][:26]:<28} ⟷ [{ns[b]:>3}] {labels[b][:26]}")
        shown += 1
        if shown >= 22:
            break
    print(f"   （≥0.75 的**互不重叠**对共这么多：{shown}；全部 ≥0.75 的有序对数 {len(pairs)}）")

    # ── 四、亚运会那一族：手挑的对照 ──────────────────────────────────────
    print("\n四、对照：**亚运会那一族的议题**（同一件事被切成几个议题的最明显例子）")
    idx = [i for i, l in enumerate(labels) if "亚运" in l]
    idx.sort(key=lambda i: -ns[i])
    for i in idx[:14]:
        others = [(S[i, j], j) for j in range(len(ids)) if j != i]
        best = max(others)[0] if others else 0
        print(f"   [{ns[i]:>3}] {labels[i][:30]:<32} 最近邻 {best:.3f}")
    print(f"   ⇒ 「亚运」字样的议题共 {len(idx)} 个，覆盖 {ns[idx].sum()} 条条目")

    # ── 五、把它们按当前阈值真并一遍，看上一层有多大 ──────────────────────
    if len(idx) > 1:
        sub = np.ix_(idx, idx)
        Ss = S[sub]
        print(f"   这一族内部的**两两余弦**：最大 {Ss[Ss < 1].max() if Ss.size > 1 else 0:.3f} · "
              f"中位 {np.median(Ss[Ss < 1]):.3f}（当前阈值 0.80）")
        print("   ⇒ 中位远低于阈值 ⇒ 它们**不是被聚类切开的，是本来就没有「同一个质心」** ——")
        print("     各条报道讲的是不同比赛、不同人，向量层看不出它们同属一个赛会。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
