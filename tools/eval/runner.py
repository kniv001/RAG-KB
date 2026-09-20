# -*- coding: utf-8 -*-
"""
**跑一个模型，出一份报告** —— 平台的执行层。

与管线尺子（`tools/ruler/`）的关键区别：
  · 管线尺子量的是**语料与管线**（检索排第几、分段切得准不准）—— 换模型不影响它
  · 这里量的是**模型本身**（分类判断、引用纪律、诚实性、速度）—— 换个模型就该重跑

所以本模块的一切都以 `--model` 为一等参数，报告里也**必须带模型名** ——
跟管线尺子必须带语料戳是同一条规矩。

跑法是**真实链路**（`/api/chat/stream`，agent 模式）：因为要判的正是端到端行为，
复刻一个"生成"没有意义（模型侧没有便宜的影子）。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
BENCH_DIR = os.path.join(HERE, "benches")

SCHEMA = "eval/bench@1"


def benches():
    if not os.path.isdir(BENCH_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(BENCH_DIR) if f.endswith(".json"))


def load_bench(name):
    p = os.path.join(BENCH_DIR, name if name.endswith(".json") else name + ".json")
    if not os.path.exists(p):
        raise SystemExit(f"没有这个基准：{name}（现有：{', '.join(benches())}）")
    b = json.load(open(p, encoding="utf-8"))
    if b.get("schema") != SCHEMA:
        raise SystemExit(f"{name} 的 schema 是 {b.get('schema')}，本模块要 {SCHEMA}")
    return b


def audit_bench(b):
    """基准自检：题型必须认识、判据必须存在、id 唯一。**不过就拒跑**。"""
    from . import judges
    bad = []
    ids = [c.get("id") for c in b["cases"]]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        bad.append(f"id 重复：{sorted(dup)}")
    for c in b["cases"]:
        k = c.get("kind")
        if k not in judges.PASS:
            bad.append(f"{c.get('id')} 题型不认识：{k}（应为 {'/'.join(judges.PASS)}）")
        if not c.get("q"):
            bad.append(f"{c.get('id')} 没有题目")
    kinds = {}
    for c in b["cases"]:
        kinds[c.get("kind")] = kinds.get(c.get("kind"), 0) + 1
    lines = [f"基准 {b['name']}（schema {SCHEMA}）· {len(b['cases'])} 题 · "
             + "　".join(f"{k} {v}" for k, v in sorted(kinds.items()))]
    if bad:
        lines.append(f"  ✗ {len(bad)} 处问题：" + "；".join(bad[:5]))
        raise SystemExit("\n".join(lines) + "\n基准没通过审计 —— 先修，别跑数")
    lines.append(f"  ✓ 题型与判据齐备，id 唯一")
    return "\n".join(lines)


def describe(b):
    return f"{b['name']:<18}{len(b['cases']):>3} 题　{b.get('note','')[:64]}"


# ── 采集与判分**分开** ────────────────────────────────────────────────────
# 为什么必须分开：**改判据不该重跑模型**。第一版把它们揉在一个 run() 里，
# 结果为了收紧两条判据又跑了一遍 15 题（≈5 分钟 + 一轮随机波动），
# 而且两次的分不可比。判分是纯函数：给定落盘结果 → 分数。
import subprocess  # noqa: E402

from . import judges  # noqa: E402

_RUNS = os.path.join(HERE, "_runs")


def run_path(bench, model, tag=""):
    return os.path.join(_RUNS, f"{bench}__{model.replace(':', '-').replace('/', '-')}{tag}.json")


def collect(bench, model, limit=0, tag=""):
    """采集：跑模型，落盘。"""
    out = run_path(bench, model, tag)
    cmd = ["node", os.path.join(HERE, "collect.mjs"),
           "--bench", bench, "--model", model, "--out", out]
    if limit:
        cmd += ["--limit", str(limit)]
    # 与其它探针一致：从仓库根跑（探针里的路径都是仓库根相对的）
    subprocess.run(cmd, cwd=os.path.dirname(TOOLS), check=False)
    if not os.path.exists(out):
        raise SystemExit(f"采集没产出结果：{out}")
    return out


def score(bench_name, model, paths=None, quiet=False):
    """判分：在**已落盘的结果**上跑判据，不碰模型。paths 给多个时取「全部通过」。"""
    b = load_bench(bench_name)
    if not quiet:
        print(audit_bench(b), "\n")
    paths = paths or [run_path(bench_name, model)]
    for p in paths:
        if not os.path.exists(p):
            raise SystemExit(f"没有落盘结果：{p}\n先跑：python tools/eval.py collect {bench_name} --model {model}")
    runs = [json.load(open(p, encoding="utf-8")) for p in paths]
    first = runs[0]

    per = []
    for i, res in enumerate(first["results"]):
        rows = [judges.judge(r["results"][i]["kind"], r["results"][i]["answer"],
                             r["results"][i]["sources"] or [], r["results"][i]["q"])
                for r in runs]
        need = judges.PASS[res["kind"]]
        ok = all(judges.passes(r, res["kind"]) for r in rows)
        per.append({"id": res["id"], "kind": res["kind"], "q": res["q"], "ok": ok,
                    "rows": rows, "answer": res["answer"], "n_sources": len(res["sources"] or []),
                    "ms": res["ms"], "ttftMs": res.get("ttftMs", 0), "error": res.get("error")})
        detail = "　".join(f"{k}={v}" for k, v in rows[0].items() if not k.startswith("_"))
        print(f"  {'✅' if ok else '❌'} [{res['kind']:<9}] {res['q'][:30]:<32}{detail}")

    print(f"\n—— {b['name']} · 模型 {model}"
          + (f" · {len(paths)} 次取全通过" if len(paths) > 1 else "") + " ——")
    kinds = {}
    for p in per:
        k = kinds.setdefault(p["kind"], [0, 0])
        k[1] += 1
        k[0] += p["ok"]
    for k, (ok, n) in sorted(kinds.items()):
        print(f"  {k:<10}{ok}/{n} = {100*ok/n:.0f}%")
    ok = sum(1 for p in per if p["ok"])
    print(f"  {'总计':<10}{ok}/{len(per)} = {100*ok/len(per):.0f}%")

    lat = sorted(p["ms"] for p in per if p["ms"])
    tt = sorted(p["ttftMs"] for p in per if p["ttftMs"])
    if lat:
        print(f"\n  耗时中位 {lat[len(lat)//2]/1000:.1f}s"
              + (f"　TTFT 中位 {tt[len(tt)//2]/1000:.1f}s" if tt else ""))
    bad = [p for p in per if not p["ok"]]
    if bad:
        print(f"\n  没通过的 {len(bad)} 题：")
        for p in bad:
            need = judges.PASS[p["kind"]]
            why = "、".join(f"{k}={p['rows'][0].get(k)}" for k in need if not p["rows"][0].get(k))
            print(f"    ❌ [{p['kind']}] {p['q'][:30]}　（{why or '多轮不一致'}）")
            head = (p["answer"] or "").replace("\n", " ")[:86]
            print(f"       答：{head or p['error'] or '(空)'}")
            if p["rows"][0].get("_数字未落地"):
                print(f"       未落地的数字：{p['rows'][0]['_数字未落地']}")
    return per


def run(bench_name, model, limit=0, repeat=1):
    """采集 N 次 → 判分（N>1 时取「全部通过」）。"""
    paths = []
    for r in range(repeat):
        tag = f"__r{r+1}" if repeat > 1 else ""
        print(f"—— 采集 {bench_name} × {model}" + (f"（第 {r+1}/{repeat} 次）" if repeat > 1 else "") + " ——")
        paths.append(collect(bench_name, model, limit, tag))
        print()
    return score(bench_name, model, paths)
