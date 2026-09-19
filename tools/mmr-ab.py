# -*- coding: utf-8 -*-
"""
**冗余过滤**（近重复去重的检索侧形态）：在 RRF 选出候选后，把"和已选中的块高度重叠"的踢掉，
名额让给后面的块。

为什么现在才值得测：休眠清单里「近重复去重」的判死依据是"**整块完全相同**只有 6%"。
但实测到的重复是**部分重叠** —— 同一句落在相邻两块的末尾/开头，两块共享约三成文本，
向量因此很接近，**一起被召回、互相挤占名额**。整块级去重抓不到这种。

而且今天的 `top-k 8→16` 把局面变了：候选池 48、名额 24 —— **从"填不满"变成"抢名额"**，
去重等于多出有效名额。这正好是那条休眠项被激活的条件。

两种重叠判据都试：
  · `cos`  —— 两块嵌入的余弦（≥ 阈值就踢）
  · `字面`  —— 两块正文的字符 bigram Jaccard（≥ 阈值就踢）
用法：python tools/mmr-ab.py
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
            out.append(s[i]); i += 1
    return "".join(out)


def psql_rows(sql):
    f = os.path.join(HERE, "_mmr.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    return [[unescape(x) for x in ln.rstrip("\r").split("\t")]
            for ln in raw.split("\n") if ln.strip()]


def embed(texts):
    out = []
    for i in range(0, len(texts), 8):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": texts[i:i + 8]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            out += json.load(r)["embeddings"]
    return out


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)


def bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    K, CAP = 16, 24
    live = json.load(io.open(os.path.join(HERE, "_multihop-live.json"), encoding="utf-8"))
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    rows = psql_rows("SELECT c.id, c.content, c.seq, d.name FROM chunks c "
                     "JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    bodies = [r[1] for r in rows]
    meta = [(r[2], r[3]) for r in rows]
    vecs = json.load(io.open(os.path.join(HERE, "_corpus_vecs.json"), encoding="utf-8"))["vecs"]
    norm = lambda s: re.sub(r"\s+", "", s)

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

    allq = []
    for rec in live:
        allq += rec.get("queries") or [rec["q"]]
    uniq = sorted(set(allq))
    qv = {}
    for i in range(0, len(uniq), 8):
        for q, v in zip(uniq[i:i + 8], embed(uniq[i:i + 8])):
            qv[q] = v

    variants = [("无过滤", None, 0), ("cos≥0.90 踢", "cos", 0.90), ("cos≥0.85 踢", "cos", 0.85),
                ("字面≥0.45 踢", "lit", 0.45), ("字面≥0.30 踢", "lit", 0.30)]
    res = {v[0]: [0, 0] for v in variants}
    dedup = {v[0]: [] for v in variants}
    for case, rec in zip(cfg["cases"], live):
        gs = groups(case)
        scored = {}
        for q in (rec.get("queries") or [rec["q"]]):
            for rank, (_, j) in enumerate(sorted(((cos(qv[q], v), j)
                                                 for j, v in enumerate(vecs)),
                                                 reverse=True)[:K]):
                scored[j] = scored.get(j, 0) + 1 / (60 + rank)
        ranked = [j for j, _ in sorted(scored.items(), key=lambda x: -x[1])]
        for name, kind, thr in variants:
            keep = []
            for j in ranked:
                dup = False
                for i in keep:
                    if kind == "cos":
                        if cos(vecs[j], vecs[i]) >= thr:
                            dup = True; break
                    elif kind == "lit":
                        a, b = bigrams(bodies[j]), bigrams(bodies[i])
                        if a and b and len(a & b) / len(a | b) >= thr:
                            dup = True; break
                if not dup:
                    keep.append(j)
            top = set(keep[:CAP])
            ok = all(any(j in top for j in g) for g in gs)
            res[name][0] += ok
            res[name][1] += 1
            dedup[name].append((len(ranked), len(top)))
    print(f"题 {len(cfg['cases'])}　每条查询 top-k={K}　名额上限={CAP}\n")
    print(f"{'冗余过滤':<14}{'全中率':>12}{'候选→入选（均值）':>20}")
    for name, _, _ in variants:
        ok, n = res[name]
        cand = sum(c for c, _ in dedup[name]) / len(dedup[name])
        kept = sum(k for _, k in dedup[name]) / len(dedup[name])
        print(f"{name:<14}{f'{ok}/{n} = {100*ok/n:.0f}%':>12}{f'{cand:.0f} → {kept:.0f}':>20}")
    print("\n（去重是把重叠的踢掉、名额让给后面的块 —— 不占额外预算，只换选取）")


if __name__ == "__main__":
    main()
