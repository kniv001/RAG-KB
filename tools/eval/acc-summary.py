# -*- coding: utf-8 -*-
"""**转正验收的读数**：把 N 轮落盘按**转正表那一套判据**算一遍（同一口径才可比）。

判据（见子台账「转正」一节，别改口径 —— 换了口径的数与历史不可比）：
  · **伤害** = `说没有` **且** 引用了资料值（这是主判据；"说没有↓"是**错的分母**）
  · **未标引用的资料值** 合计
  · **护栏** = `ungrounded` 那几道仍须声明"没有"
  · **乙类 `有引用`** = `grounded` 还**在不在用资料**（总分里有"如实声明没有"这条路，
    材料被削光也能拿通过 ⇒ 必须单看这一列）
  · 通过数（按题型分）
用法：python data/_acc_summary.py [tag]   # tag 默认 __acc
"""
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from eval import judges                                          # noqa: E402

TAG = sys.argv[1] if len(sys.argv) > 1 else "__acc"
BENCH = sys.argv[2] if len(sys.argv) > 2 else "answer-quality"
files = sorted(glob.glob(os.path.join(ROOT, "tools", "eval", "_runs",
                                      f"{BENCH}__qwen3-4b__*{TAG}.json")))


def replay(x):
    """回放的识别：秒回 + 无思考（`runner` 每轮前清缓存，但仍要防混进样本）。"""
    return (x.get("ms") or 0) < 5000 and len(x.get("thinking") or "") <= 50


def rows_of(path):
    d = json.load(io.open(path, encoding="utf-8"))
    return [x for x in d.get("results", [])
            if (x.get("answer") or "").strip() and not replay(x)]


def metrics(rows):
    uv = sum(sum(len(v) for _, v, _ in judges.uncited_values(x["answer"], x.get("sources") or []))
             for x in rows)
    harm = [x["id"] for x in rows
            if judges.declares_missing(x["answer"])
            and judges.uncited_values(x["answer"], x.get("sources") or [])]
    byk, passed, fails = {}, 0, []
    for x in rows:
        k = x.get("kind")
        # ⚠️ **必须带上 `targets`** —— 不带的话 `multi` 那几道的
        # "覆盖了所有靶事实"会是 None，而 `passes` **跳过 None** ⇒ 新判据**静默失效**。
        # （2026-09-29：给题型加判据的那天忘了同步这里，是本项目最熟悉的一族错。）
        row = judges.judge(k, x["answer"], x.get("sources") or [], x.get("q") or "",
                           (x.get("stats") or {}).get("cites"), x.get("targets"))
        bad = [q for q in (judges.PASS.get(k) or []) if row.get(q) is False]
        a = byk.setdefault(k, [0, 0, 0])
        a[0] += 1
        a[1] += 0 if bad else 1
        a[2] += 1 if judges.declares_missing(x["answer"]) else 0
        passed += 0 if bad else 1
        if bad:
            fails.append((x["id"], k, bad))
    cited = [x for x in rows if x.get("kind") == "grounded"]
    cited_ok = sum(1 for x in cited
                   if (x.get("sources") or []) and re.search(r"\[\d+\]", x["answer"]))
    return {"uv": uv, "harm": harm, "passed": passed, "n": len(rows), "byk": byk,
            "fails": fails, "cited": f"{cited_ok}/{len(cited)}"}


print(f"落盘：{len(files)} 份（{TAG}）")
tot = {"uv": 0, "harm": [], "passed": 0, "n": 0, "cited_ok": 0, "cited_n": 0}
for p in files:
    rs = rows_of(p)
    m = metrics(rs)
    tot["uv"] += m["uv"]
    tot["harm"] += m["harm"]
    tot["passed"] += m["passed"]
    tot["n"] += m["n"]
    co, cn = m["cited"].split("/")
    tot["cited_ok"] += int(co)
    tot["cited_n"] += int(cn)
    print(f"\n{os.path.basename(p)}")
    print(f"  通过 {m['passed']}/{m['n']}　未标引用值 {m['uv']}　伤害 {len(m['harm'])}{m['harm']}"
          f"　乙类有引用 {m['cited']}")
    print("  " + "　".join(f"{k} {v[1]}/{v[0]}" for k, v in sorted(m["byk"].items())))
    for i, k, bad in m["fails"]:
        print(f"    ❌ {i} [{k}] {bad}")

print(f"\n===== 合计（{len(files)} 轮）=====")
print(f"  通过 {tot['passed']}/{tot['n']}　未标引用值 {tot['uv']}　伤害 {len(tot['harm'])}"
      f"{tot['harm']}　乙类有引用 {tot['cited_ok']}/{tot['cited_n']}")
