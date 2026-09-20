# -*- coding: utf-8 -*-
"""
**判据层** —— 一个判据一个函数，签名统一 `(C, cs, result, cfg) -> dict`，返回的字典直接进报告。

现有判据：
  · `all-hit`   全中率 —— 答案要的块**全**被召回才算命中（多跳的判据；比"有没有一个命中"严格）
  · `recall`    逐靶召回率（分母是靶子组数）
  · 天花板（`ceiling`）**不是判据，是每次都要附的一列** ——
    "靶子是否全在候选池里"。不附它就会把"池子没有靶子"误读成"精排器不行"（2026-09-20 踩过）
  · `windowdiff` 分段质量（另一族，见下）

报告里**必须**同时出现 `全中率 / 逐靶召回 / 天花板 / 池子大小` 四列 —— 少一列就会误判归因。
"""


def all_hit(C, cs, picked_sets, cfg=None):
    """picked_sets：每题一个"最终入选下标集合"。返回逐题结果与汇总。"""
    per = []
    for c, picked in zip(cs.cases, picked_sets):
        gs = [t.group for t in c["_targets"]]
        hits = [any(j in picked for j in g) for g in gs]
        per.append(hits)
    ok = sum(1 for h in per if h and all(h))
    n = len(per)
    return {"全中率": f"{ok}/{n} = {100*ok/max(1,n):.0f}%", "_ok": ok, "_n": n, "_per": per}


def recall(C, cs, picked_sets, cfg=None):
    tot = hit = 0
    for c, picked in zip(cs.cases, picked_sets):
        for t in c["_targets"]:
            tot += 1
            hit += t.hit(picked)
    return {"逐靶召回": f"{100*hit/max(1,tot):.0f}%"}


def self_hit(C, cs, orders, cfg=None):
    """自检索：**排第一才算好**（与多跳的"全中"是两种要求，别混）。

    为什么不是"在不在 top-k"：切块切得准，换来的是**排名**不是可达性 ——
    本系统取 top-8×3 并不要求排第一，所以这条判据是**更严的一把**，
    它回答的是"切得对不对"，不是"找不找得到"。
    """
    h1 = hk = n = 0
    for c, order in zip(cs.cases, orders):
        gs = [t.group for t in c["_targets"]]
        if not gs or not gs[0]:
            continue
        n += 1
        pos = next((r for r, j in enumerate(order) if any(j in g for g in gs)), None)
        h1 += (pos == 0)
        hk += (pos is not None)
    return {"排第一": f"{h1}/{n} = {100*h1/max(1,n):.0f}%",
            "进 top-k": f"{hk}/{n} = {100*hk/max(1,n):.0f}%"}


def ceiling(C, cs, pool_sets, cfg=None):
    ok = 0
    for c, pool in zip(cs.cases, pool_sets):
        if all(any(j in pool for j in t.group) for t in c["_targets"]):
            ok += 1
    n = len(cs.cases)
    return {"天花板": f"{ok}/{n} = {100*ok/max(1,n):.0f}%"}


# ── 分段族 ──────────────────────────────────────────────────────────────
SEG_K = 5     # WindowDiff 的窗口大小（段）。**别改** —— 改了与历史数字不可比。


def segmentation(C, cs, bounds, cfg=None):
    """分段族的判据：逐窗口 WindowDiff + 必切召回 + 误切 + 未标注 + 粒度。

    四个数各管一件事，缺一个就会误读：
      · **WD**   —— 与"必须切"的距离（越小越好）；比"误切数÷切点数"稳，刷不动
      · **必切召回** —— must 里有多少被切到了（±1 段算中，边界抖动不算错）
      · **误切** —— 切在 forbidden 区间里的刀数（切在代码碎片里最典型）
      · **未标注** —— 既不是 must 也不是 optional、也不在 forbidden 里的刀（细了但不犯错）
    """
    B = sorted({int(b) for b in bounds})
    wds, rec, forb, unmarked, ncut = [], [0, 0], 0, 0, 0
    for w in cs.cases:
        lo, hi = w["lo"], w["hi"]
        n = hi - lo
        win = [b for b in B if lo < b <= hi]
        must = w["must"]
        ds = windowdiff([m - lo for m in must], [b - lo for b in win], n, SEG_K)
        wds.append(ds)
        forb += sum(1 for b in win if any(f[0] <= b <= f[1] for f in w["forbidden"]))
        unmarked += sum(1 for b in win if b not in must and b not in w["optional"]
                        and not any(f[0] <= b <= f[1] for f in w["forbidden"]))
        rec[0] += sum(1 for m in must if any(abs(b - m) <= 1 for b in win))
        rec[1] += len(must)
        ncut += len(win)
    R = rec[0] / max(1, rec[1])
    return {"必切召回": f"{100*R:.0f}%",
            "WD": f"{sum(wds)/max(1,len(wds)):.2f}",
            "误切": str(forb), "未标注": str(unmarked),
            "粒度": f"{round(100*ncut/max(1,sum(w['hi']-w['lo'] for w in cs.cases)))}/百段"}


def windowdiff(ref, hyp, n, k=None):
    """WindowDiff：分段质量的公认指标（Pevzner & Hearst 2002）。

    为什么不用"误切数 ÷ 切点数"：那个自检时被打回过 —— **"每段一刀"能刷到 0.62**，
    比"每 5 段一刀"的 0.083 还高。WindowDiff 比较的是**窗口内边界条数**，
    对"多切"和"少切"同等惩罚，刷不动。

    **窗口口径与历史实现逐字一致**（`i` 从 1 到 n-k，除数 n-k）——
    差一个窗口就会让"全不切"从 0.62 变 0.60，与历史数字接不上（2026-09-20 对过）。
    """
    k = SEG_K if k is None else k
    if n - k <= 0:
        return 0.0
    errs = 0
    for i in range(1, n - k + 1):
        r = sum(1 for x in ref if i < x <= i + k)
        h = sum(1 for x in hyp if i < x <= i + k)
        errs += (r != h)
    return errs / (n - k)
