# -*- coding: utf-8 -*-
"""
多跳检索尺子：**答案需要几个块同时被召回**。

为什么要有它：现有两套题（15 单跳 / 15 难题）判的是"**有没有一个**命中"，
都已满分（15/15、14/15）—— **饱和**。而真实提问常常要**凑齐两三个块**才能回答，
"全中率"会随跳数相乘地掉，这才是还有区分度的地方。

判据：
  · **全中率**（每个靶子都进 top-k）
  · **逐靶召回**（单个靶子进 top-k 的比例）

靶子标成 `doc` + `targets_seq`（块的 seq）；判定时**同文档里任何含该块文字的块**命中都算中 ——
这批块有重叠重复，只认那一个块会把「召回了它的重复块」误判成失败。

检索复刻**向量通道**（`ctx + 正文`）；不含关键词通道与查询改写 ⇒ 是下界。
接到真实链路的那半在 `multihop-live-probe.mjs` / `multihop-live-compare.py`。

用法：python tools/multihop-probe.py [k1 k2 ...，默认 4 8 12 24]
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
EMBED = "bge-m3"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
VCACHE = os.path.join(HERE, "_corpus_vecs.json")
QFILE = os.path.join(HERE, "multihop-questions.json")
BS = chr(92)
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    """**单遍**反转义。顺序 replace 会把 LaTeX 的 `\\text{其中}` 变成「制表符 + ext{…}」，字段切歪。"""
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
        # **先剥掉行尾的 \r**：COPY 输出是 CRLF，按 \n 切完会在最后一个字段留下回车，
        # 于是文档名比对永远为假、所有靶子都匹配不上（2026-09-20 踩过，全场 0 命中）
        out.append([unescape(x) for x in line.rstrip("\r").split("\t")])
    return out


def embed(rows, mode="ctx+body"):
    """索引文本的三种形态（只差这一个因素，差值就是**语境行的贡献**）：
      · `ctx+body`（线上现状）· `body`（对照）· `ctx`（只用语境行）——
    第三种是拿来分辨**"ctx 被正文淹没"**这个解释的：正文约 600 字、ctx 约 25 字，权重差 20 倍。
    """
    ids = [r[0] for r in rows]
    if mode == "body":
        texts = [r[2] for r in rows]
    elif mode == "ctx":
        texts = [(r[1] or r[2]) for r in rows]
    else:
        texts = [((r[1] + "\n" + r[2]) if r[1] else r[2]) for r in rows]
    cache = VCACHE.replace(".json", {"ctx+body": ".json", "body": "-noctx.json",
                                     "ctx": "-ctxtonly.json"}[mode])
    if os.path.exists(cache):
        d = json.load(io.open(cache, encoding="utf-8"))
        if d.get("ids") == ids:
            return d["vecs"]
    vecs = []
    for i in range(0, len(texts), 8):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": EMBED, "input": texts[i:i + 8]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            vecs += json.load(r)["embeddings"]
    io.open(cache, "w", encoding="utf-8").write(json.dumps({"ids": ids, "vecs": vecs}))
    return vecs


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ks = [int(x) for x in sys.argv[1:] if x.isdigit()] or [4, 8, 12, 24]
    topk = max(ks)
    cfg = json.load(io.open(QFILE, encoding="utf-8"))

    rows = psql_rows("SELECT c.id, coalesce(c.ctx,''), c.content, c.doc_id, c.seq, d.name "
                     "FROM chunks c JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    ids = [r[0] for r in rows]
    texts = [(r[1] + "\n" + r[2]) if r[1] else r[2] for r in rows]   # 检索用文本（可能带 ctx）
    bodies = [r[2] for r in rows]                                    # **正文**：靶子匹配只用它
    meta = [(r[3], r[4], r[5]) for r in rows]          # doc_id, seq, doc_name
    # `--fuse`：**两个索引各排一遍、再 RRF 融合**。
    # 现状是把 ctx 拼在正文前面（"拼接"是很弱的融合：600 字里 25 字，占了 4% 的权重）。
    # 分开建索引再融合，才真正给了 ctx 一票。
    fuse = "--fuse" in sys.argv
    mode = "ctx" if "--ctx-only" in sys.argv else ("body" if "--no-ctx" in sys.argv else "ctx+body")
    if fuse:
        vecs = embed(rows, "body")
        vecs_ctx = embed(rows, "ctx")
        mode = "RRF(正文, 语境行)"
    else:
        vecs = embed(rows, mode)
    pos = {cid: i for i, cid in enumerate(ids)}
    norm = lambda s: re.sub(r"\s+", "", s)

    def targets_of(case):
        """→ 每个靶子一组可接受的块下标（同文档 + 含该块**正文**的这段文字）。

        **键必须取正文，不能取检索用文本**：后者带 ctx 时，键会变成 ctx 的开头，
        于是"有 ctx"和"没 ctx"两次测的靶子集不一样，数字没法比（2026-09-20 踩过：
        同一改动一边 48% 一边 56%，就是这里来的）。
        """
        grp_all = []
        for sq in case["targets_seq"]:
            t = next((i for i, m in enumerate(meta)
                      if m[2] == case["doc"] and str(m[1]) == str(sq)), None)
            if t is None:
                grp_all.append([])
                continue
            key = norm(bodies[t])[:40]
            grp = [i for i, m in enumerate(meta)
                   if m[2] == case["doc"] and (key in norm(bodies[i]) or norm(bodies[i]) in key)]
            grp_all.append(grp or [t])
        return grp_all

    print(f"全库 {len(ids)} 块　题 {len(cfg['cases'])}　k 扫描 {ks}　索引文本={mode}\n")
    per = []
    for c in cfg["cases"]:
        want = targets_of(c)
        if any(not g for g in want):
            print(f"  ⚠ 有靶子没匹配上：{c['q'][:36]}")
        # 查询嵌入（每次都发，不走缓存）
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": EMBED, "input": [c["q"]]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            qv = json.load(r)["embeddings"][0]
        if fuse:
            r1 = [j for _, j in sorted(((cos(qv, v), j) for j, v in enumerate(vecs)),
                                       reverse=True)[:topk]]
            r2 = [j for _, j in sorted(((cos(qv, v), j) for j, v in enumerate(vecs_ctx)),
                                       reverse=True)[:topk]]
            sc = {}
            for rank, j in enumerate(r1):
                sc[j] = sc.get(j, 0) + 1 / (60 + rank)
            for rank, j in enumerate(r2):
                sc[j] = sc.get(j, 0) + 1 / (60 + rank)
            order = [j for j, _ in sorted(sc.items(), key=lambda x: -x[1])][:topk]
        else:
            order = [j for _, j in sorted(((cos(qv, v), j) for j, v in enumerate(vecs)),
                                          reverse=True)[:topk]]
        rk = [min([order.index(j) + 1 for j in grp if j in order], default=0) for grp in want]
        per.append((c["q"], rk))
        sys.stdout.flush()

    print(f"{'k':>5}{'全中率':>12}{'逐靶召回':>10}")
    for k in ks:
        nh = sum(1 for _, rk in per if all(0 < r <= k for r in rk))
        tr = sum(1 for _, rk in per for r in rk if 0 < r <= k)
        tn = sum(len(rk) for _, rk in per)
        print(f"{k:>5}{f'{nh}/{len(per)} = {100*nh/len(per):.0f}%':>12}"
              f"{f'{100*tr/max(tn,1):.0f}%':>10}")
    print("\n逐题（靶数 @ top-%d）：" % ks[-1])
    for q, rk in per:
        print(f"  {' '.join(f'{1 if 0<r<=ks[-1] else 0}/{1}' for r in rk)}　{rk}　{q[:40]}")


if __name__ == "__main__":
    main()
