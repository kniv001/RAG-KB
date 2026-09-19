# -*- coding: utf-8 -*-
"""
按**真实链路进提示词的那一批**给多跳题打分。

用 `done` 事件的 `sources`（完整对象，带 `docName` + `seq`），**不是** `retrieve` 事件里
每次查询只报前 5 条的那份显示用列表 —— 后者量不出"每条查询看多宽"这类改动（前 5 条不变）。

用法：python tools/multihop-live-score.py
"""
import io
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    live = json.load(io.open(os.path.join(HERE, "_multihop-live.json"), encoding="utf-8"))
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    if not any("final" in r for r in live):
        print("这次 live 记录里没有 `final`（done 事件）—— 需要用新版 multihop-live-probe.mjs 重跑")
        return

    ok = 0
    sizes = []
    fail = []
    for case, rec in zip(cfg["cases"], live):
        final = {(s.get("docName"), s.get("seq")) for s in (rec.get("final") or [])}
        if not final:
            print(f"  ⚠ 没有 final：{case['q'][:34]}")
            continue
        sizes.append(len(final))
        want = [(case["doc"], t["seq"]) for t in case["targets"]]
        hit = sum(1 for w in want if w in final)
        good = hit == len(want)
        ok += good
        if not good:
            fail.append((case["q"], hit, len(want), sorted(
                (s.get("seq") for s in (rec.get("final") or [])
                 if s.get("docName") == case["doc"]))))
        print(f"  {'✅' if good else '◐' if hit else '❌'} 靶 {hit}/{len(want)}　"
              f"进提示词 {len(final)} 段　{case['q'][:36]}")

    n = len(sizes)
    print(f"\n—— 真实链路（进提示词的那一批，n={n}）——")
    print(f"  **全中率 {ok}/{n} = {100*ok/n:.0f}%**")
    print(f"  每题的来源数：中位 {sorted(sizes)[len(sizes)//2]}，范围 {min(sizes)}~{max(sizes)}"
          f"　分布 {dict(sorted(Counter(sizes).items()))}")
    if fail:
        print("\n  未全中的：")
        for q, h, t, seqs in fail:
            print(f"    {h}/{t}　{len(seqs)} 段来自目标文档 {q[:30]}")
    print("\n对照：复刻路径（向量+RRF、上限 24）在 top-k=16 时是 72%、top-k=8 时是 36%")


if __name__ == "__main__":
    main()
