# -*- coding: utf-8 -*-
"""
DP 优势边界 · 扩语料之后重测。

上一轮（语料 39 段不同内容）的结论是「DP 没有优势，它的优势全部来自重复内容」，
但当时的语料只有两篇文章，候选之间本来就一样 —— 那种语料量不出规模效应。

现在语料是 556 段不同内容（56 篇联网抓取，覆盖 40 多个方向），重跑同一套对打，
并把**候选上限**与**预算**两维一起扫 —— 因为决定「选块问题存不存在」的是
「候选总 token 是否超过预算」，而不是语料大小本身。

用法：python tools/dp-selection-scale-probe.py
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
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scale_probe.sql")

CHARS_PER_TOKEN = 1.5
POOL = 30
MAX_DISTANCE = 0.60
SHORT_BUDGET = 10240 - 4096 - 1200     # 短对话：块能拿到的全部预算
LONG_BUDGET = 3000                     # 长对话：被摘要/召回片段/历史挤掉一截
TIGHT_BUDGET = 2000

# 与第一轮同样的 18 条真实提问，外加 10 条针对新语料的问题 ——
# 后者才有「候选全都不同」的池子，正是要量的那个边界
QUESTIONS = None  # 运行时从库里取


def est(n):
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
        print("psql 失败:", p.stderr.decode("utf-8", "backslashreplace")[:500])
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
        if it["md5"] not in seen:
            seen.add(it["md5"])
            out.append(it)
    return out


def distinct_value(kept):
    seen, val = set(), 0.0
    for it in kept:
        if it["md5"] not in seen:
            seen.add(it["md5"])
            val += it["value"]
    return val, len(seen)


def selfcheck():
    random.seed(11)
    for _ in range(150):
        n = random.randint(1, 11)
        items = [{"tok": random.randint(1, 40), "value": random.uniform(.1, 1.),
                  "md5": str(i)} for i in range(n)]
        budget = random.randint(10, 120)
        dp = knapsack(items, budget)
        if sum(i["tok"] for i in dp) > budget:
            return False
        best = max((sum(items[i]["value"] for i in comb)
                    for r in range(n + 1)
                    for comb in itertools.combinations(range(n), r)
                    if sum(items[i]["tok"] for i in comb) <= budget), default=0.0)
        if abs(sum(i["value"] for i in dp) - best) > 1e-9:
            return False
    return True


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"[自证] DP 对拍 150 组：{'✅ 一致' if selfcheck() else '❌ 不符'}\n")

    qs = [l.strip() for l in psql(
        "SELECT DISTINCT content FROM messages WHERE role='user' "
        "AND length(content) BETWEEN 4 AND 60 ORDER BY content DESC LIMIT 18;"
    ).splitlines() if l.strip()]
    qs += ['HTTP/2 的多路复用是怎么回事？', 'Redis 的 RDB 和 AOF 有什么区别？',
           'Kubernetes 的 Pod 调度是怎么做的？', 'Docker 的 namespace 和 cgroup 分别管什么？',
           'G1 垃圾回收器怎么工作？', 'Python 的 GIL 是什么？',
           'Rust 的所有权规则是什么？', '布隆过滤器为什么会误判？',
           '一致性哈希的虚拟节点解决什么问题？', '模型量化 int8 和 4bit 差别在哪？']

    pools = {}
    for q in qs:
        v = literal(embed(q))
        out = psql(f"SELECT id, md5(content), length(content), "
                   f"round((embedding <=> {v})::numeric, 4) FROM chunks "
                   f"WHERE embedding <=> {v} <= {MAX_DISTANCE} "
                   f"ORDER BY embedding <=> {v} LIMIT {POOL};")
        items = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) < 4:
                continue
            try:
                items.append({"id": p[0], "md5": p[1], "tok": est(int(p[2])),
                              "value": 1.0 - float(p[3])})
            except ValueError:
                continue
        if items:
            pools[q] = items

    total_distinct_all = psql("SELECT count(*), count(distinct md5(content)) FROM chunks;").strip()
    print(f"语料：{total_distinct_all}（总段 / 不同内容）")
    print(f"提问 {len(pools)} 条\n")

    scen = [("现状 12段/短对话", 12, SHORT_BUDGET),
            ("12段/长对话", 12, LONG_BUDGET),
            ("12段/很长的对话", 12, TIGHT_BUDGET),
            ("20段/短对话", 20, SHORT_BUDGET),
            ("30段/短对话", 30, SHORT_BUDGET),
            ("30段/长对话", 30, LONG_BUDGET)]

    for label, cap, budget in scen:
        tot = {k: 0.0 for k in "ABCD"}
        sat = 0
        for items0 in pools.values():
            items = items0[:cap]
            if sum(i["tok"] for i in items) <= budget:
                sat += 1
            tot["A"] += distinct_value(prefix_cut(items, budget))[0]
            tot["B"] += distinct_value(skip_greedy(items, budget))[0]
            tot["C"] += distinct_value(knapsack(items, budget))[0]
            tot["D"] += distinct_value(prefix_cut(dedup(items), budget))[0]
        base = tot["A"]
        pct = lambda x: 100 * (x - base) / max(base, 1e-9)
        print(f"【{label}】候选≤{cap}段 / 预算 {budget}　装得下 {sat}/{len(pools)} 条")
        print(f"    现状前缀截断 {base:6.2f} | 跳贪 {tot['B']:6.2f} ({pct(tot['B']):+5.1f}%)"
              f" | DP背包 {tot['C']:6.2f} ({pct(tot['C']):+5.1f}%)"
              f" | 去重+截断 {tot['D']:6.2f} ({pct(tot['D']):+5.1f}%)")
        # 池子里候选是否全都不同 —— 「去重还有没有用」的判据
        same = sum(1 for i0 in pools.values() for i in [i0[:cap]]
                   if len({x["md5"] for x in i}) < len(i))
        print(f"    候选池里有重复内容的题数：{same}/{len(pools)}")


if __name__ == "__main__":
    main()
