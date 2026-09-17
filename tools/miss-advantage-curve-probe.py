# -*- coding: utf-8 -*-
"""
「不漏」的优势在什么规模上显现 —— 把它测成一条曲线。

上一轮的判据是「10 道题命中几个」，天花板太低（块索引 8~10/10），显不出差异。
但「不漏」本来就不该由题目数量决定，而该由**一道题需要覆盖多少个源块**决定：

  同样预算下能装下多少个不同源块（上一轮实测）
    块   320 token/条 → 4944 预算约 15 个源块
    句    44 token/条 → 同预算约 70 个源块
  于是分界点应当在 k ≈ 15 附近：需要覆盖的源块超过它，块索引就开始漏。

测法刻意贴近真实链路：**k 条子查询取并集**（系统的规划器正是把复合问题拆成多条
子查询各查一次，再合并）—— 而不是拿一个拼起来的长问句去查一次。每条的相似度取
最大值作为该候选的分数，再在预算内按渐进限流选取。

用法：python tools/miss-advantage-curve-probe.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(HERE, "_curve.sql")
EMBED = "bge-m3"
CPT = 1.5
BUDGETS = [1500, 3000, 4944]
KS = [2, 4, 8, 16, 24]
SETS_PER_K = 3


def psql(sql):
    with open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE], capture_output=True, env=env)
    if p.returncode != 0:
        print("psql:", p.stderr.decode("utf-8", "backslashreplace")[:300])
        sys.exit(1)
    return p.stdout.decode("utf-8", "replace")


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def embed_all(texts, tag=""):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]})["embeddings"])
        if tag and i % 1600 == 0:
            print(f"    嵌入 {tag} {i}/{len(texts)}", flush=True)
    return np.array(out, dtype=np.float32)


def norm(a):
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def sentences(text, lo=12):
    parts = re.split(r"(?<=[。！？!?；;\n])\s*", text)
    out, buf = [], ""
    for p in parts:
        buf += p
        if len(buf) >= lo:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def pick(sim, items, budget, cap_start=1):
    order = np.argsort(-sim)
    used, out, tok, chosen = {}, [], 0.0, set()
    cur = cap_start
    while True:
        added = False
        for i in order:
            i = int(i)
            if i in chosen:
                continue
            it = items[i]
            if used.get(it["src"], 0) >= cur:
                continue
            t = len(it["text"]) / CPT
            if tok + t > budget:
                continue
            chosen.add(i)
            used[it["src"]] = used.get(it["src"], 0) + 1
            out.append(i)
            tok += t
            added = True
        if not added or cur >= 20:
            break
        cur += 1
    return out, tok


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    rows = psql("SELECT id, doc_id, content FROM chunks;")
    chunks = []
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            chunks.append({"id": p[0], "doc": p[1], "text": p[2], "src": p[0]})
    sents = []
    for c in chunks:
        for s in sentences(c["text"]):
            sents.append({"doc": c["doc"], "text": s, "src": c["id"]})
    print(f"全库：块 {len(chunks)}，句 {len(sents)}")
    print(f"平均 token/条：块 {np.mean([len(c['text']) for c in chunks])/CPT:.0f}，"
          f"句 {np.mean([len(s['text']) for s in sents])/CPT:.0f}\n")

    # 目标文档：取名字能当主题用的那些（去掉重复副本与后缀）
    docrows = psql("SELECT id, name FROM documents;")
    docs = {}
    for line in docrows.splitlines():
        p = line.split("\t")
        if len(p) >= 2:
            name = re.sub(r"\.md$", "", p[1])
            name = re.sub(r"\s*[-–—|_].*$", "", name).strip()
            if name and p[0] not in docs:
                docs[p[0]] = name
    # 同一名字的重复副本只留一个
    seen, targets = set(), []
    for did, name in docs.items():
        if name in seen or len(name) < 4:
            continue
        seen.add(name)
        targets.append((did, name))
    targets = sorted(targets, key=lambda x: x[1])
    print(f"可用目标文档 {len(targets)} 篇\n")

    cv = norm(embed_all([c["text"] for c in chunks], "块"))
    sv = norm(embed_all([s["text"] for s in sents], "句"))
    tv = norm(embed_all([n for _, n in targets], "主题"))

    rng = np.random.default_rng(20260917)
    colls = {"块": (chunks, cv), "句": (sents, sv)}
    for budget in BUDGETS:
        print(f"—— 预算 {budget} token ——")
        print(f"{'k':>4}{'组数':>6}" + "".join(f"{k+' 覆盖':>12}" for k in colls)
              + f"{'块 全中':>9}{'句 全中':>9}")
        for k in KS:
            if k > len(targets):
                continue
            cov = {n: [] for n in colls}
            full = {n: 0 for n in colls}
            for s in range(SETS_PER_K):
                idxs = rng.choice(len(targets), size=k, replace=False)
                tgt_docs = {targets[i][0] for i in idxs}
                sims = np.max(tv[idxs] @ cv.T, axis=0)
                sims_s = np.max(tv[idxs] @ sv.T, axis=0)
                for name, (items, sim) in (("块", (chunks, sims)), ("句", (sents, sims_s))):
                    sel, _ = pick(sim, items, budget)
                    got = len({items[i]["doc"] for i in sel} & tgt_docs)
                    cov[name].append(got / k)
                    if got == k:
                        full[name] += 1
            print(f"{k:>4}{SETS_PER_K:>6}" + "".join(
                f"{100*np.mean(cov[n]):>11.0f}%" for n in colls)
                + f"{full['块']:>7}/{SETS_PER_K}{full['句']:>7}/{SETS_PER_K}")
        print()


if __name__ == "__main__":
    main()
