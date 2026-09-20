# -*- coding: utf-8 -*-
"""
**距离门槛**的 A/B：门槛是不是那个把 ctx 收益吃掉的闸门？

来龙去脉：真实链路上开 ctx 只注入 13 段、关掉却注入 24 段，而全中率 52% vs 68%。
查下来是两条线索合起来：
  · 带 ctx 的块**彼此更散**（块间平均余弦 0.468 vs 0.494）⇒ 离查询也更远
  · 真实链路有 **0.60 的距离门槛**（实测 ctx 关闭时全部来源 ≤0.549，一条没滤）
⇒ 假设：**开了 ctx 之后有块被门槛滤掉了**，而滤掉的代价（池子小）盖过了排序的收益。

测法：在**带 ctx 的嵌入**上，按不同门槛模拟"过滤 + 截 24"，看池子大小与全中率。
（复刻路径：向量 + 每条查询 top-k + RRF，与 topk-ab 同一套，只是多一道门槛。）

用法：python tools/threshold-ab.py [k，默认 8]
"""
import io
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _veccache                                              # noqa: E402
OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == BS and i + 1 < len(s):
            out.append(_ESC.get(s[i + 1], BS + s[i + 1])); i += 2
        else:
            out.append(s[i]); i += 1
    return "".join(out)


def psql_rows(sql):
    f = os.path.join(HERE, "_th.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    return [[unescape(x) for x in ln.rstrip("\r").split("\t")]
            for ln in raw.split("\n") if ln.strip()]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    K = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    live = json.load(io.open(os.path.join(HERE, "_multihop-live.json"), encoding="utf-8"))
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    rows = psql_rows("SELECT c.content, c.seq, c.ctx, d.name, c.id FROM chunks c "
                     "JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    bodies = [r[0] for r in rows]
    meta = [(r[1], r[3]) for r in rows]
    import re
    norm = lambda s: re.sub(r"\s+", "", s)
    vecs = _veccache.load("_corpus_vecs.json", [r[4] for r in rows], rebuild=False)

    def groups(case):
        gs = []
        for t in case["targets"]:
            idx = next((i for i, m in enumerate(meta)
                        if m[1] == case["doc"] and m[0] == str(t["seq"])), None)
            if idx is None:
                gs.append([]); continue
            key = norm(bodies[idx])[:40]
            gs.append([i for i, m in enumerate(meta)
                       if m[1] == case["doc"] and norm(bodies[i]).startswith(key[:20])] or [idx])
        return gs

    def emb(qs):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": qs}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.load(r)["embeddings"]

    def cos(a, b):
        s = sum(x * y for x, y in zip(a, b))
        return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)

    allq = sorted({q for rec in live for q in (rec.get("queries") or [rec["q"]])})
    qv = {}
    for i in range(0, len(allq), 8):
        for q, v in zip(allq[i:i + 8], emb(allq[i:i + 8])):
            qv[q] = v

    # 门槛用**距离**（1-cos），与线上 `distance` 一致
    ths = [0.60, 0.70, 0.80, 1.01]
    res = {t: [0, 0, []] for t in ths}
    for case, rec in zip(cfg["cases"], live):
        gs = groups(case)
        scored = {}
        dist = {}
        for q in (rec.get("queries") or [rec["q"]]):
            for rank, (c, j) in enumerate(sorted(((cos(qv[q], v), j)
                                                  for j, v in enumerate(vecs)), reverse=True)[:K]):
                scored[j] = scored.get(j, 0) + 1 / (60 + rank)
                dist[j] = min(dist.get(j, 9), 1 - c)
        ranked = [j for j, _ in sorted(scored.items(), key=lambda x: -x[1])]
        for t in ths:
            keep = [j for j in ranked if dist[j] <= t][:24]
            ok = all(any(j in keep for j in g) for g in gs)
            res[t][0] += ok
            res[t][1] += 1
            res[t][2].append(len(keep))
    print(f"题 {len(cfg['cases'])}　每条查询 top-k={K}（带 ctx 的嵌入）\n")
    print(f"{'距离门槛':>10}{'全中率':>14}{'进池段数（中位/范围）':>22}")
    for t in ths:
        ok, n, sizes = res[t]
        sizes = sorted(sizes)
        label = "无门槛" if t > 1 else f"{t:.2f}"
        print(f"{label:>10}{f'{ok}/{n} = {100*ok/n:.0f}%':>14}"
              f"{f'{sizes[len(sizes)//2]} / {sizes[0]}~{sizes[-1]}':>22}")
    print("\n（若放宽门槛同时抬高全中率与池子大小，说明门槛就是吃收益的那道闸）")


if __name__ == "__main__":
    main()
