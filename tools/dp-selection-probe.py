# -*- coding: utf-8 -*-
"""
DP 有没有优势 · 之一：预算内选块。

现状是前缀截断（PromptBudget.keepWithin：遇到第一个放不下的就整体停下）。
DP 能精确解 0-1 背包。本探针在同一批真实候选集上对打五种策略，
并且**先自证**（DP 与暴力枚举对拍），再报数 —— 免得拿一个实现错误的结论去做决策。

指标为什么用「去重后的价值」而不只是价值：
  候选池里 86% 是重复内容（同一篇文章被联网检索重复入库十次），
  重复段各自计入 Σ(1-距离) 会让「多塞几段重复」看起来像收益。
  真正该衡量的是**不同内容**拿到了多少。

用法：python tools/dp-selection-probe.py
"""
import itertools
import json
import math
import os
import random
import subprocess
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434/api/embed"
MODEL = "bge-m3"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dp_probe.sql")

BUDGET_TOKENS = 10240 - 4096 - 1200   # 与 answer() 同一套数
CHARS_PER_TOKEN = 1.5
POOL = 30
MAX_DISTANCE = 0.60
MAX_CONTEXTS = 12


def est_tokens(n):
    return math.ceil(n / CHARS_PER_TOKEN)


def psql(sql):
    with open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE],
                       capture_output=True, env=env)
    if p.returncode != 0:
        print("psql 失败:", p.stderr.decode("utf-8", "backslashreplace")[:600])
        sys.exit(1)
    return p.stdout.decode("utf-8", "replace")


def embed(text):
    body = json.dumps({"model": MODEL, "input": [text]}).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embeddings"][0]


def literal(v):
    return "'[" + ",".join(f"{x:.7f}" for x in v) + "]'"


# ---------------- 选择策略 ----------------

def prefix_cut(items, budget):
    kept, used = [], 0
    for it in items:
        if used + it["tok"] > budget:
            break
        kept.append(it)
        used += it["tok"]
    return kept


def skip_greedy(items, budget):
    kept, used = [], 0
    for it in items:
        if used + it["tok"] <= budget:
            kept.append(it)
            used += it["tok"]
    return kept


def knapsack(items, budget):
    prev = [0.0] * (budget + 1)
    take = [[False] * (budget + 1) for _ in range(len(items))]
    for i, it in enumerate(items):
        w, v = it["tok"], it["value"]
        cur = prev[:]
        if w <= budget:
            for b in range(w, budget + 1):
                cand = prev[b - w] + v
                if cand > cur[b]:
                    cur[b] = cand
                    take[i][b] = True
        prev = cur
    kept, b = [], budget
    for i in range(len(items) - 1, -1, -1):
        if take[i][b]:
            kept.append(items[i])
            b -= items[i]["tok"]
    kept.reverse()
    return kept


def dedup(items):
    seen, out = set(), []
    for it in items:
        if it["md5"] in seen:
            continue
        seen.add(it["md5"])
        out.append(it)
    return out


def distinct_value(kept):
    """去重后的价值 —— 同一段内容无论被选中几次只算一次"""
    seen, val = {}, 0.0
    for it in kept:
        if it["md5"] not in seen:
            seen[it["md5"]] = it["value"]
            val += it["value"]
    return val, len(seen)


# ---------------- 自证：DP 与暴力枚举对拍 ----------------

def selfcheck():
    random.seed(7)
    bad = 0
    for t in range(200):
        n = random.randint(1, 12)
        items = [{"tok": random.randint(1, 40), "value": random.uniform(0.1, 1.0),
                  "md5": str(i)} for i in range(n)]
        budget = random.randint(10, 120)
        dp = knapsack(items, budget)
        dval = sum(i["value"] for i in dp)
        if sum(i["tok"] for i in dp) > budget:
            bad += 1
            continue
        best = 0.0
        for r in range(n + 1):
            for comb in itertools.combinations(range(n), r):
                if sum(items[i]["tok"] for i in comb) <= budget:
                    best = max(best, sum(items[i]["value"] for i in comb))
        if abs(dval - best) > 1e-9:
            bad += 1
    print(f"[自证] DP 对拍 200 组随机用例：{'✅ 与暴力枚举完全一致' if bad == 0 else f'❌ {bad} 组不符'}\n")
    return bad == 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not selfcheck():
        sys.exit(1)

    qs = [l.strip() for l in psql(
        "SELECT DISTINCT content FROM messages WHERE role='user' "
        "AND length(content) BETWEEN 4 AND 60 ORDER BY content DESC LIMIT 18;"
    ).splitlines() if l.strip()]
    print(f"真实提问 {len(qs)} 条，预算 {BUDGET_TOKENS} token\n")

    # 先取一次候选集，之后在各场景里重复使用（嵌入调用最贵）
    pools = {}
    for q in qs:
        v = literal(embed(q))
        out = psql(
            f"SELECT id, md5(content), length(content), "
            f"round((embedding <=> {v})::numeric, 4) "
            f"FROM chunks WHERE embedding <=> {v} <= {MAX_DISTANCE} "
            f"ORDER BY embedding <=> {v} LIMIT {POOL};")
        items = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) < 4:
                continue
            try:
                items.append({"id": p[0], "md5": p[1], "chars": int(p[2]),
                              "dist": float(p[3]), "tok": est_tokens(int(p[2])),
                              "value": 1.0 - float(p[3])})
            except ValueError:
                continue
        if items:
            pools[q] = items

    # 场景 = (候选上限, 给块的预算)。上限来自 rank() 的 maxContexts=12；
    # 预算在长对话里会被摘要/召回片段/历史挤掉一截，所以多测一档紧预算。
    scenarios = [("现状 12段/短对话", 12, BUDGET_TOKENS),
                 ("12段/长对话(预算被挤)", 12, 3000),
                 ("不限段/短对话", POOL, BUDGET_TOKENS),
                 ("不限段/紧预算", POOL, 3000)]

    for label, cap, budget in scenarios:
        tot = {k: 0.0 for k in "ABCDE"}
        sat = 0
        for q, items0 in pools.items():
            items = items0[:cap]
            # 预算相对候选是否 binding —— 也就是「选块这个问题存不存在」
            if sum(i["tok"] for i in items) <= budget:
                sat += 1
            tot["A"] += distinct_value(prefix_cut(items, budget))[0]
            tot["B"] += distinct_value(skip_greedy(items, budget))[0]
            tot["C"] += distinct_value(knapsack(items, budget))[0]
            tot["D"] += distinct_value(prefix_cut(dedup(items), budget))[0]
            tot["E"] += distinct_value(knapsack(dedup(items), budget))[0]
        base = tot["A"]
        print(f"\n【{label}】候选上限 {cap} 段 / 预算 {budget} token"
              f"　（{sat}/{len(pools)} 条提问的候选全部装得下 —— 装得下就没有选块问题）")
        for k, name in [("A", "前缀截断(现状)"), ("B", "跳过式贪心"), ("C", "DP 背包"),
                        ("D", "去重+前缀截断"), ("E", "去重+DP 背包")]:
            print(f"    {name:<16}{tot[k]:>7.2f}   相对现状 {100*(tot[k]-base)/max(base,1e-9):+6.1f}%")


if __name__ == "__main__":
    main()
