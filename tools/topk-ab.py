# -*- coding: utf-8 -*-
"""
`top-k-per-query` 的 A/B：**用真实规划器查询**，复刻应用的融合同上限。

为什么要这样测：真实链路的事件只报**前 5 条来源**（`sourceNames()` 给 UI 用的），
所以 live 探针量不出"每条查询取 8 还是 12"的变化。这里改成：
  · 查询用 `_multihop-live.json` 里**规划器真实产出的那批**（不是原问句）
  · 每条查询取 top-k（8 / 12 / 16），**RRF 融合**后截到 `max-contexts`（24）
  · 判据 = 多跳**全中率**

关键性质：**改 top-k 是预算中性的** —— 最终装进提示词的还是那 24 个名额，
只是从更宽的池子里挑。RRF 的"多查询互相印证"也正是靠这个起作用。

用法：python tools/topk-ab.py [k1 k2 ...，默认 8 12 16] [--cap 24]
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == BS and i + 1 < len(s):
            out.append(_ESC.get(s[i + 1], BS + s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def psql_rows(sql):
    f = os.path.join(HERE, "_tk.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    return [[unescape(x) for x in ln.rstrip("\r").split("\t")]
            for ln in raw.split("\n") if ln.strip()]


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cap = 24
    if "--cap" in sys.argv:
        cap = int(sys.argv[sys.argv.index("--cap") + 1])
    # 取 k 参数时要**先剔掉 --cap 的值**，否则它会被当成一个 k（踩过：8/12/16/24 变成 8/12/16/24/24）
    argv = list(sys.argv[1:])
    if "--cap" in argv:
        i = argv.index("--cap")
        del argv[i:i + 2]
    ks = [int(x) for x in argv if not x.startswith("--")] or [8, 12, 16]

    live = json.load(io.open(os.path.join(HERE, "_multihop-live.json"), encoding="utf-8"))
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    rows = psql_rows("SELECT c.id, coalesce(c.ctx,''), c.content, c.seq, d.name "
                     "FROM chunks c JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    texts = [(r[1] + "\n" + r[2]) if r[1] else r[2] for r in rows]
    bodies = [r[2] for r in rows]
    meta = [(r[3], r[4]) for r in rows]                     # seq, doc_name
    cache = os.path.join(HERE, "_corpus_vecs.json")
    vecs = json.load(io.open(cache, encoding="utf-8"))["vecs"]
    norm = lambda s: re.sub(r"\s+", "", s)

    def groups(case):
        gs = []
        tg = case.get("targets") or [{"seq": s} for s in case.get("targets_seq", [])]
        for item in tg:
            sq = item["seq"]
            t = next((i for i, m in enumerate(meta) if m[1] == case["doc"] and m[0] == str(sq)), None)
            if t is None:
                gs.append([]); continue
            key = norm(bodies[t])[:40]
            gs.append([i for i, m in enumerate(meta)
                       if m[1] == case["doc"] and norm(bodies[i]).startswith(key[:20])]
                      or [t])
        return gs

    # 先把所有查询嵌入一次
    allq = []
    for rec in live:
        allq += rec.get("queries") or [rec["q"]]
    uniq = sorted(set(allq))
    qv = {}
    for i in range(0, len(uniq), 8):
        emb = post("/api/embed", {"model": "bge-m3", "input": uniq[i:i + 8]})["embeddings"]
        for q, v in zip(uniq[i:i + 8], emb):
            qv[q] = v

    print(f"题 {len(cfg['cases'])}　融合上限 max-contexts={cap}　每条查询取 top-k ∈ {ks}\n")
    res = {k: [0, 0] for k in ks}          # [全中, 题数]
    for case, rec in zip(cfg["cases"], live):
        gs = groups(case)
        row = []
        for k in ks:
            sc = {}
            for q in (rec.get("queries") or [rec["q"]]):
                order = sorted(((cos(qv[q], v), j) for j, v in enumerate(vecs)),
                               reverse=True)[:k]
                for rank, (_, j) in enumerate(order):
                    sc[j] = sc.get(j, 0) + 1 / (60 + rank)
            top = set(j for j, _ in sorted(sc.items(), key=lambda x: -x[1])[:cap])
            ok = all(any(j in top for j in g) for g in gs)
            res[k][0] += ok
            res[k][1] += 1
            row.append("✅" if ok else "❌")
        print(f"  {' '.join(f'{v}(k={k})' for v, k in zip(row, ks))}　{case['q'][:38]}")
    print(f"\n{'每条查询 top-k':>14}{'全中率':>12}")
    for k in ks:
        print(f"{k:>14}{f'{res[k][0]}/{res[k][1]} = {100*res[k][0]/res[k][1]:.0f}%':>12}")


if __name__ == "__main__":
    main()
