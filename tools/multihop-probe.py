# -*- coding: utf-8 -*-
"""
多跳检索尺子：**答案需要几个块同时被召回**。

为什么要有它：现有两套题（15 单跳 / 15 难题）判的是"**有没有一个**命中"，
已经 15/15、14/15 —— **饱和**。而真实提问常常要**凑齐两三个块**才能回答，
"全中率"会随跳数相乘地掉，这才是还有区分度的地方。

判据：
  · **全中率**（所有 targets 都进 top-k）
  · **逐靶召回**（单个 target 进 top-k 的比例）
  · 每题的最差名次（最后一个靶子排第几）

靶子标在**段号**上（与 seg-ruler 的窗口标注同一套编号），探针按分块边界映射到块，
再对**全库**做向量检索（复刻检索的向量通道；不含关键词通道与查询改写 ⇒ 是下界）。

用法：python tools/multihop-probe.py [topk，默认 12]
"""
import io
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
EMBED = "bge-m3"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
QFILE = os.path.join(HERE, "multihop-questions.json")


def psql_rows(sql):
    f = os.path.join(HERE, "_mh.sql")
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
        for a, b in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(a, b)
        out.append(line)
    return out


def embed(texts):
    out = []
    for i in range(0, len(texts), 8):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": EMBED, "input": texts[i:i + 8]}).encode("utf-8"),
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
    topk = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    cfg = json.load(io.open(QFILE, encoding="utf-8"))
    bounds = json.load(io.open(os.path.join(HERE, "_bounds_chunking.json"),
                               encoding="utf-8"))["bounds"]
    segs = json.load(io.open(os.path.join(HERE, "..", "data", "_bintree-segs.json"),
                             encoding="utf-8"))["segs"]   # 靶子标在段号上

    # 段 → 块（0-based 块序号）：块的起点由 bounds 给出
    def seg2chunk(seg):
        k = 0
        for i, b in enumerate(bounds):
            if seg >= b:
                k = i + 1
        return k

    # 全库块（id + 检索用文本：ctx + 正文，与线上一致）
    rows = psql_rows("SELECT id, coalesce(ctx,''), content FROM chunks ORDER BY id")
    ids, texts = [], []
    for r in rows:
        p = r.split("\t")
        if len(p) < 3:
            continue
        ids.append(p[0])
        texts.append((p[1] + "\n" + p[2]) if p[1] else p[2])
    print(f"全库 {len(texts)} 块　top-k={topk}\n")
    vecs = embed(texts)
    pos = {cid: i for i, cid in enumerate(ids)}

    # 该文档的块 id（按 seq），用来把段号映射成 chunk id
    docrows = psql_rows("SELECT id FROM chunks WHERE doc_id=(SELECT id FROM documents WHERE name="
                        f"'{cfg['doc']}') ORDER BY seq")
    docids = [x.strip() for x in docrows if x.strip()]

    # 靶子 = **所有含该段文字的块**：这批块有重叠重复，只认映射到的那一个会把
    # 「召回了它的重复块」误判成失败（假失败）。
    import re as _re
    norm = lambda s: _re.sub(r"\s+", "", s)

    def chunks_containing(text, docids_):
        key = norm(text)[:40]
        if len(key) < 16:
            return []
        return [pos[cid] for cid in docids_ if key in norm(texts[pos[cid]])
                or norm(texts[pos[cid]]) in key]

    allhit = [0, 0]
    per = []
    for c in cfg["cases"]:
        want = []
        for seg in c["targets"]:
            hits = chunks_containing(segs[seg - 1], docids) if seg - 1 < len(segs) else []
            if not hits:
                ci = seg2chunk(seg)
                hits = [pos[docids[ci]]] if ci < len(docids) else []
            want.append(hits)          # 每个靶子是一组可接受的块
        qv = embed([c["q"]])[0]
        order = [j for _, j in sorted(((cos(qv, v), j) for j, v in enumerate(vecs)),
                                      reverse=True)[:topk]]
        ok = [any(j in order for j in grp) for grp in want]
        ranks = [min([order.index(j) + 1 for j in grp if j in order], default=0) for grp in want]
        allhit[0] += all(ok)
        allhit[1] += 1
        per.append((c["q"], len(want), sum(ok), ranks))
        mark = "✅" if all(ok) else ("◐" if any(ok) else "❌")
        print(f"  {mark} 靶 {sum(ok)}/{len(want)}　名次 {ranks}　{c['q'][:44]}")
    n = allhit[1]
    tr = sum(p[2] for p in per) / max(sum(p[1] for p in per), 1)
    print(f"\n—— 结果（top-{topk}）——")
    print(f"  **全中率 {allhit[0]}/{n} = {100*allhit[0]/n:.0f}%**　逐靶召回 {100*tr:.0f}%")
    print("  （判据：全中率。现有单跳集是 15/15、难题集 14/15 —— 都能满分）")


if __name__ == "__main__":
    main()
