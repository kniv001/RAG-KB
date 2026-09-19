# -*- coding: utf-8 -*-
"""
同口径对比：**真实链路 vs 向量复刻**，用的还是规划器改写后那批查询。

真实链路（`multihop-live-probe.mjs` 收下来的）报的是每次查询的**前 5 条**来源
（`docName#seq`），所以复刻也必须按同一口径算：**每次查询取前 5、再按查询求并**。
只有口径一致，"真实链路比复刻高多少"才是个有意义的数 —— 那差额就是
**关键词通道 + RRF + 距离门槛**贡献的。

用法：python tools/multihop-live-compare.py
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
LIVE = os.path.join(HERE, "_multihop-live.json")
VCACHE = os.path.join(HERE, "_corpus_vecs.json")
TOPQ = 5          # 真实链路每次查询只报前 5 条 —— 复刻按同一口径


_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    """**单遍**反转义。

    顺序 replace 是错的：先做 `\\\\`→`\\` 再做 `\\t`→制表符，会把正文里 LaTeX 的
    `\\text{其中}` 变成「制表符 + ext{其中}」—— 于是字段被切歪（报错长成
    `int('ext{其中}...')`，那个报错就是这样来的）。
    """
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
    f = os.path.join(HERE, "_mlc.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        out.append("\t".join(unescape(x) for x in line.split("\t")))
    return out


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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    live = json.load(io.open(LIVE, encoding="utf-8"))
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    bounds = json.load(io.open(os.path.join(HERE, "_bounds_chunking.json"),
                               encoding="utf-8"))["bounds"]
    segs = json.load(io.open(os.path.join(HERE, "..", "data", "_bintree-segs.json"),
                             encoding="utf-8"))["segs"]

    def seg2chunk(seg):
        k = 0
        for i, b in enumerate(bounds):
            if seg >= b:
                k = i + 1
        return k

    # **按 `文档名#seq` 全串比** —— seq 是按文档编号的，只比 seq 会让别的文档的同号块冒充靶子
    want_sets = [[f"{cfg['doc']}#{seg2chunk(s)}" for s in c["targets"]] for c in cfg["cases"]]

    # 真实链路
    real_hit = 0
    print("—— 真实链路（每次查询前 5 条来源）——")
    for rec, want in zip(live, want_sets):
        seqs = {str(s) for s in rec.get("sources", [])}
        hit = sum(1 for w in want if w in seqs)
        real_hit += (hit == len(want))
        print(f"  {'✅' if hit == len(want) else '◐' if hit else '❌'} 靶 {hit}/{len(want)}"
              f"　查询 {len(rec.get('queries', []))}　{rec['q'][:36]}")

    # 复刻：用**同一批规划器查询**，同一口径（每次前 5）
    ids, texts, vecs = None, None, None
    rows = psql_rows("SELECT id, coalesce(ctx,''), content, doc_id, seq FROM chunks ORDER BY id")
    ids, texts, meta = [], [], []
    for r in rows:
        p = r.split("\t")
        if len(p) < 5:
            continue
        ids.append(p[0])
        texts.append((p[1] + "\n" + p[2]) if p[1] else p[2])
        meta.append((p[3], int(p[4])))
    d = json.load(io.open(VCACHE, encoding="utf-8"))
    vecs = d["vecs"] if d.get("ids") == ids else embed(texts)
    pos = {cid: i for i, cid in enumerate(ids)}
    names = [x.strip() for x in psql_rows("SELECT id || '\t' || name FROM documents")]
    docname = dict(n.split("\t", 1) for n in names if "\t" in n)

    rep_hit = 0
    print("\n—— 向量复刻（同查询、同口径：每次前 5）——")
    for rec, want in zip(live, want_sets):
        qs = rec.get("queries") or [rec["q"]]
        seqs = set()
        for q in qs:
            qv = embed([q])[0]
            top = sorted(((cos(qv, v), j) for j, v in enumerate(vecs)), reverse=True)[:TOPQ]
            for _, j in top:
                did, sq = meta[j]
                seqs.add(f"{docname.get(did, '')}#{sq}")
        hit = sum(1 for w in want if w in seqs)
        rep_hit += (hit == len(want))
        print(f"  {'✅' if hit == len(want) else '◐' if hit else '❌'} 靶 {hit}/{len(want)}　{rec['q'][:36]}")

    n = len(live)
    print(f"\n—— 对比（n={n}）——")
    print(f"  真实链路全中率 {real_hit}/{n} = {100*real_hit/n:.0f}%")
    print(f"  向量复刻全中率 {rep_hit}/{n} = {100*rep_hit/n:.0f}%")
    print(f"  差（关键词通道 + RRF + 门槛的贡献）：{100*(real_hit-rep_hit)/n:+.0f} 点")


if __name__ == "__main__":
    main()
