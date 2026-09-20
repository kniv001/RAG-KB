# -*- coding: utf-8 -*-
"""
把多次 live 跑按臂汇总，判断"ctx 开 / 关"到底有没有差别。

为什么要多次：live 尺子是**无偏但高噪声**的 —— 同配置重复跑，进池段数 13↔24、分数 ±1~2 题。
单次定不了 3 题级的差异，所以每臂跑 3 次再合并。

文件约定：`tools/_live_ctxon_run*.json` / `tools/_live_noctx_run*.json`（live-probe 的产物）。

用法：python tools/live-ab-summary.py
"""
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def score(path, cfg):
    live = json.load(io.open(path, encoding="utf-8"))
    ok = n = 0
    sizes = []
    for case, rec in zip(cfg["cases"], live):
        final = {(s.get("docName"), s.get("seq")) for s in (rec.get("final") or [])}
        if not final:
            continue
        sizes.append(len(final))
        want = [(case["doc"], t["seq"]) for t in case["targets"]]
        ok += all(w in final for w in want)
        n += 1
    return ok, n, sorted(sizes)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    arms = {}
    for tag, pat in (("ctx 开", "_live_ctxon_run*.json"), ("ctx 关", "_live_noctx_run*.json")):
        files = sorted(glob.glob(os.path.join(HERE, pat)))
        rows = []
        for f in files:
            ok, n, sizes = score(f, cfg)
            rows.append((os.path.basename(f), ok, n, sizes[len(sizes) // 2] if sizes else 0))
        arms[tag] = rows

    print(f"{'臂':<8}{'跑次':<28}{'全中率':>10}{'进池中位':>10}")
    for tag, rows in arms.items():
        for name, ok, n, med in rows:
            print(f"{tag:<8}{name:<28}{f'{ok}/{n} = {100*ok/n:.0f}%':>10}{med:>10}")
        if rows:
            so = sum(r[1] for r in rows)
            sn = sum(r[2] for r in rows)
            print(f"{'':<8}{'—— 合并 ——':<28}{f'{so}/{sn} = {100*so/sn:.0f}%':>10}")
        print()

    # 两臂合并后的简单检验（正态近似，双尾）
    if all(arms.values()):
        a = arms["ctx 开"]; b = arms["ctx 关"]
        oa, na = sum(r[1] for r in a), sum(r[2] for r in a)
        ob, nb = sum(r[1] for r in b), sum(r[2] for r in b)
        pa, pb = oa / na, ob / nb
        p = (oa + ob) / (na + nb)
        se = (p * (1 - p) * (1 / na + 1 / nb)) ** .5
        z = (pb - pa) / se if se else 0
        print(f"两臂合并：ctx 开 {100*pa:.0f}%（{na} 题）　ctx 关 {100*pb:.0f}%（{nb} 题）")
        print(f"  差 {100*(pb-pa):+.0f} 点　z = {z:.2f}　"
              f"{'显著（|z|>1.96）' if abs(z) > 1.96 else '**不显著**'}")
        print("\n（live 是有噪声的尺子：单臂 1 次跑的差可能到 ±2 题，所以看合并值）")


if __name__ == "__main__":
    main()
