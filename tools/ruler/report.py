# -*- coding: utf-8 -*-
"""
**报告层** —— 一个数字脱离它的尺子就没法引用（2026-09-20：同一条判死一天翻两次，模型没变）。

所以每份报告**固定带四样**：
  · 尺子名（题目集 + 检索器 + 参数）
  · 语料戳（`corpus.stamp`）
  · 四列判据：`全中率 / 逐靶召回 / 天花板 / 池子大小`
  · 重复次数与范围（真身那类高噪声尺子，单次定不了 3 题级差异）

`compare(arms)` 做配对比较：**逐题只在两臂之间比**（不跨题平均），
报出 `A独赢 / B独赢 / 平`，并给一个符号检验的粗略显著度 —— 避免把 1 题的差当成收益。
"""
import math

from . import judges


def one(C, cs, retr, rec=None, **cfg):
    """跑一条臂，返回 {判据名: 值} + 逐题细节。"""
    picked, pools, orders = retr(C, cs, want_pool=True, rec=rec, **cfg)
    out = {}
    if cs.judge == "self-hit":
        out.update(judges.self_hit(C, cs, orders))
    else:
        out.update(judges.all_hit(C, cs, picked))
        out.update(judges.recall(C, cs, picked))
    if pools is not None:
        out.update(judges.ceiling(C, cs, pools))
    sizes = sorted(len(p) for p in picked)
    out["池子大小"] = f"{sizes[len(sizes)//2]}（{sizes[0]}~{sizes[-1]}）"
    out["_picked"] = picked
    out["_order"] = orders
    return out


def arm_label(name, cfg):
    if not cfg:
        return name
    bits = []
    for key in ("k", "cap", "kind", "thresh", "mmr", "query_source"):
        if key in cfg and cfg[key] is not None:
            v = cfg[key]
            if key == "query_source" and v == "planner":
                continue
            bits.append(f"{key}={v}")
    return f"{name}({', '.join(bits)})" if bits else name


# 报告列的顺序：判据本身在前，**天花板与池子大小永远跟着** ——
# 少了天花板就会把"池子里没有靶子"误读成"精排器不行"（2026-09-20 踩过）。
CANON = ["全中率", "排第一", "进 top-k", "逐靶召回", "天花板", "池子大小"]


def table(C, cs, arms, repeats=1, rec=None):
    """arms: [(名字, retr, cfg)]。打印统一表，返回逐题结果供配对比较。"""
    print(f"\n—— {cs.name} · 判据 {cs.judge} · {C.line()} ——")
    rows, detail = [], {}
    cols = None
    for name, retr, cfg in arms:
        label = arm_label(name, cfg)
        runs = [one(C, cs, retr, rec=rec, **cfg) for _ in range(repeats)]
        if cols is None:
            cols = [c for c in CANON if c in runs[0]]
        agg = {}
        for col in cols:
            vals = [r.get(col) for r in runs if col in r]
            if not vals:
                continue
            if repeats == 1:
                agg[col] = vals[0]
            else:                       # 高噪声尺子：报「最好/最差」而不是假装收敛
                nums = [v.split(" ")[0] for v in vals]           # "17/25 = 68%"
                agg[col] = f"{vals[0]} ｜ {repeats} 次: " + " / ".join(
                    v.split("=")[-1].strip() for v in vals)
                del nums
        rows.append((label, agg))
        detail[label] = runs[0]["_picked"]
    w = max(len(r[0]) for r in rows) + 2
    print(f"  {'臂':<{w}}" + "".join(f"{c:>26}" for c in cols))
    for label, agg in rows:
        print(f"  {label:<{w}}" + "".join(f"{agg.get(c,'—'):>26}" for c in cols))
    return detail


def paired(C, cs, base_label, base, other_label, other):
    """配对比较：逐题比（不跨题平均），给出符号检验的粗略 p。"""
    win = lose = tie = 0
    for c, b, o in zip(cs.cases, base, other):
        gs = [t.group for t in c["_targets"]]
        hb = all(any(j in b for j in g) for g in gs)
        ho = all(any(j in o for j in g) for g in gs)
        if ho and not hb:
            win += 1
        elif hb and not ho:
            lose += 1
        else:
            tie += 1
    n = win + lose
    # 双侧符号检验（正态近似；n 小时只当粗略参考）
    p = 1.0
    if n:
        z = abs(win - n / 2) / math.sqrt(n / 4)
        p = math.erfc(z / math.sqrt(2))
    print(f"\n  配对：**{other_label} 独赢 {win} / {base_label} 独赢 {lose} / 平 {tie}**"
          f"　（符号检验 p≈{p:.2f}{'，不显著' if p > 0.05 else ''}）")
    return win, lose, tie, p
