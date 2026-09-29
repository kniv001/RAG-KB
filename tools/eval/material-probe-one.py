# -*- coding: utf-8 -*-
"""**按生产线**量几道候选题的"还剩几句材料" —— 扩题闸门的第一道要以此为准。

## 为什么必须有它（2026-09-29 量出来的偏差）

`bench-expand` 的材料闸数的是"**全库** top-20 里过 0.65 的句"，而系统数的是
"**召回到的那些块**里过 0.65 的句" —— 两个集合**不一样** ⇒ 闸会**高估**。
实测：`aq-g11` 闸说 ≥3 句、生产线给 **0 句** ⇒ 那道题在真跑里被答成
「本次检索未找到…的具体资料」，而它挂着 `grounded` 的标签。

⇒ 这就是本项目的老教训（**先确认"我的集合 == 系统的集合"**）在扩题这一步上的又一次。
闸只能当**粗筛**；**定稿要看生产线的读数**（`material-probe.mjs` 发完请求就断开、
不等生成，9 秒/题）。

用法：python tools/eval/material-probe-one.py "问句1" "问句2" …
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
LOG = os.path.join(REPO, "data", "app.log")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FLOOR_RE = re.compile(r"材料地板 ([\d.]+)（(.{0,20})）：过滤后剩 (\d+) 句 / (\d+) 块")


def read_since(t0):
    import glob
    import gzip
    out = {}
    for path in sorted(glob.glob(LOG + "*")):
        op = gzip.open if path.endswith(".gz") else open
        try:
            with op(path, "rt", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    m = FLOOR_RE.search(ln)
                    if m and ln[:19] >= t0:
                        out[m.group(2)] = (int(m.group(3)), int(m.group(4)))
        except Exception:
            pass
    return out


def main():
    qs = [q for q in sys.argv[1:] if q.strip()]
    if not qs:
        raise SystemExit(__doc__)
    # 造一个只有这些题的临时基准（探针按基准名找题库）
    import io
    import json
    tmp = "material-probe-tmp"
    p = os.path.join(HERE, "benches", tmp + ".json")
    io.open(p, "w", encoding="utf-8").write(json.dumps({
        "schema": "eval/bench@1", "name": tmp, "note": "临时",
        "cases": [{"id": f"c{i+1}", "kind": "grounded", "q": q} for i, q in enumerate(qs)],
    }, ensure_ascii=False, indent=1))
    try:
        subprocess.run(["node", os.path.join(TOOLS, "clear-answers.mjs")],
                       cwd=REPO, check=False, stdout=subprocess.DEVNULL)
        t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
        subprocess.run(["node", os.path.join(HERE, "material-probe.mjs"),
                        "--bench", tmp, "--model", "qwen3:4b",
                        "--wait", "9000"], cwd=REPO)
        got = read_since(t0)
        print()
        for i, q in enumerate(qs):
            v = got.get(q[:20])
            mark = "✓" if (v and v[0] >= 3) else ("△" if v and v[0] else "✗")
            print(f"  {mark} 剩 {v[0] if v else '?'} 句 / {v[1] if v else '?'} 块　{q[:46]}")
        print("\n（✓ ≥3 = 项目自己那条「有材料」的门槛；✗ = 生产口径下会被答成「库里没有」）")
    finally:
        os.unlink(p)


if __name__ == "__main__":
    main()
