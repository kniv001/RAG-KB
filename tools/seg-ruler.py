# -*- coding: utf-8 -*-
"""
分段质量的**尺子**（2026-09-19）。

为什么需要：这一整天里四次结构实验都卡在同一处 —— 8/16 vs 9/16 vs 基线 9/16 这种数
**读不出结论**，因为判据分不开两种完全不同的东西：
  · 切错了（切在代码中间、切在一条解释中间）
  · 只是**粒度更细**（并列项各算一个话题，那是合法分区）

做法：人工把真实文本里的位置标成三类 ——
  **must**（真的换了话题）／**optional**（更细也站得住，**不扣分**）／**forbidden**（切在这里就是错）

打分用 **WindowDiff**（分段评测的标准量，滑窗比较切点数，**多切与少切同等惩罚**）：
  · WD_strict：参考 = must
  · WD_lenient：参考 = must ∪ optional（切在可切点不罚 —— 这是"粒度容忍"的实现）
  · 另记「切在 forbidden 里的刀数」：最直观的那个"明显错"

**第一版尺子被自检打回**：当时用「误切数 / 切点数」当惩罚，结果「每段一刀」靠刷满召回
拿到 0.62 分，比「每 5 段一刀」的 0.083 还高 —— 切得越多分母越大。**归一化错了**，改用 WindowDiff。
（这就是尺子必须先自检的原因。）

用法：
  python tools/seg-ruler.py --selfcheck
  python tools/seg-ruler.py boundaries.json       # {"bounds": [1-based 段号...]}

【判据已搬进 tools/ruler.py】用 `python tools/ruler.py seg [--all]`（判据逐字一致，自带语料戳）。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ANN = os.path.join(HERE, "seg-ruler-annotation.json")
K = 5          # WindowDiff 的窗口大小（段）


def load():
    return json.load(io.open(ANN, encoding="utf-8"))


def win_diff(ref, hyp, n, k=K):
    """参考切点 ref 与提议切点 hyp（1-based 段号集合）的 WindowDiff，越小越好，0 为完美。"""
    if n - k <= 0:
        return 0.0
    errs = 0
    for i in range(1, n - k + 1):
        r = sum(1 for x in ref if i < x <= i + k)
        h = sum(1 for x in hyp if i < x <= i + k)
        if r != h:
            errs += 1
    return errs / (n - k)


def score(bounds, ann, verbose=False):
    B = sorted(set(int(b) for b in bounds))
    rows, wds, forb, rec, unmarked, ncut = [], [], 0, [0, 0], 0, 0
    for w in ann["windows"]:
        lo, hi = w["lo"], w["hi"]
        n = hi - lo
        win = [b for b in B if lo < b <= hi]
        must = [m["at"] for m in w["must"]]
        opt = [o["at"] for o in w["optional"]]
        ds = win_diff({m - lo for m in must}, {b - lo for b in win}, n)
        bf = sum(1 for b in win if any(f["lo"] <= b <= f["hi"] for f in w["forbidden"]))
        um = sum(1 for b in win if b not in must and b not in opt
                 and not any(f["lo"] <= b <= f["hi"] for f in w["forbidden"]))
        hm = sum(1 for m in must if any(abs(b - m) <= 1 for b in win))
        rec[0] += hm
        rec[1] += len(must)
        forb += bf
        unmarked += um
        ncut += len(win)
        wds.append(ds)
        rows.append((lo, hi, n, len(win), hm, len(must), bf, um, ds))   # n = 窗口段数
    R = rec[0] / rec[1] if rec[1] else 0.0
    WD = sum(wds) / len(wds) if wds else 0.0
    if verbose:
        print(f"{'窗口':>10}{'段数':>5}{'切点':>5}{'必切召回':>9}{'误切':>5}{'未标注':>7}{'WD':>6}")
        for lo, hi, n, nw, hm, nm, bf, um, ds in rows:
            print(f"{f'{lo+1}-{hi}':>10}{n:>5}{nw:>5}{f'{hm}/{nm}':>9}{bf:>5}{um:>7}{ds:>6.2f}")
    total_n = sum(r[2] for r in rows)
    return {"R": R, "WD": WD, "误切": forb, "未标注": unmarked, "切点": ncut,
            "段数": total_n, "粒度": round(100 * ncut / max(total_n, 1))}


def show(tag, bounds, ann):
    r = score(bounds, ann, verbose=(tag.startswith("手工")))
    print(f"{tag:<20} 必切召回 {r['R']*100:>5.0f}%　WD {r['WD']:.2f}　误切 {r['误切']:>2}　"
          f"未标注 {r['未标注']:>2}　粒度 {r['粒度']:>3}/百段")
    return r


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ann = load()
    all_idx = [i for w in ann["windows"] for i in range(w["lo"] + 1, w["hi"] + 1)]
    must_bounds = [m["at"] for w in ann["windows"] for m in w["must"]]

    if "--selfcheck" in sys.argv[:2]:
        print(f"—— 尺子自检（WindowDiff，窗口 {K} 段；越小越好）——")
        res = {}
        res["全不切"] = show("全不切（0 刀）", [], ann)
        res["全切"] = show("全切（每段一刀）", all_idx, ann)
        for k in (3, 5, 8):
            res[f"每{k}"] = show(f"每 {k} 段一刀", all_idx[::k], ann)
        res["手工"] = show("手工（只切 must）", must_bounds, ann)
        ok = (res["手工"]["WD"] < res["每3"]["WD"] < res["全切"]["WD"]
              and res["手工"]["误切"] == 0 and res["手工"]["未标注"] == 0
              and res["全不切"]["R"] == 0)
        print(f"\n  判定：手工最干净（WD 最小、误切 0、未标注 0），全切最差，全不切召回 0 ⇒ "
              f"{'✅ 尺子可用' if ok else '❌ 尺子作废'}")
        return

    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        b = json.load(io.open(sys.argv[1], encoding="utf-8"))["bounds"]
        show("给定分段", b, ann)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
