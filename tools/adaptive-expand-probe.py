# -*- coding: utf-8 -*-
"""
卷积信号用于**检索单元的自适应扩张** —— 不是用来找作者的分段。

思路：命中一个块之后，看它两侧的「边界信号」——说「还是同一件事」就继续往外扩，
说「这里是边界」就停。检索单元于是随内容自适应，而不是固定 600 字。

为什么这个用法对信号的要求恰好相反：
  · 漏报一个边界 → 多带一段相邻内容（无害）
  · 误报一个边界 → 把有用的内容挡在门外（有害）
所以只要**精确率高**就够，召回低不碍事。实测信号是 75% / 38%，正是这个方向。

对照三路：
  块        按相似度选块（现状）
  块+扩张   选块后沿信号扩张（本方案）
  扩张阈值  扫一遍，看它是不是根旋钮

判据沿用同一套：目标文档独有词覆盖率 + 命中率，两个预算。

用法：python tools/adaptive-expand-probe.py
"""
import io
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
EMBED, CPT = "bge-m3", 1.5
BUDGETS = [1500, 4944]
BS = chr(92)

TOPICS = {
    "HTTP/2": ["%HTTP 2%", "%HTTP2%", "%http系列%"],
    "Redis 持久化": ["%Redis持久化%", "%Redis两种持久化%"],
    "Kafka": ["%kafka消费组%"],
    "Docker": ["%docker容器网络%"],
    "Kubernetes": ["%Kubernetes Pod调度%"],
    "布隆过滤器": ["%布隆过滤器%"],
    "一致性哈希": ["%一致性哈希%"],
    "向量数据库": ["%向量数据库%", "%HNSW%"],
    "模型量化": ["%模型量化%", "%INT8量化%"],
    "B-tree": ["%B-tree 索引结构%"],
    "G1": ["%G1%"],
    "限流": ["%限流%"],
}
QUESTIONS = [
    ("HTTP/2", "HTTP/2 的多路复用是怎么实现的？"),
    ("Redis 持久化", "Redis 的 RDB 和 AOF 有什么区别？"),
    ("布隆过滤器", "布隆过滤器为什么会有误判，误判率怎么算？"),
    ("向量数据库", "HNSW 的 efSearch 参数控制什么？"),
    ("Kubernetes", "Pod 调度里的亲和性是怎么配置的？"),
    ("模型量化", "int8 量化和 4bit 量化的区别是什么？"),
    ("Docker", "Docker 的 bridge 网络是怎么连通的？"),
    ("G1", "G1 垃圾回收器的 Region 是怎么划分的？"),
    ("限流", "令牌桶和漏桶算法有什么区别？"),
]
MULTI = [
    ("Redis 的 AOF 和 Kafka 的日志复制在持久化思路上有什么不同？", "Redis 持久化", "Kafka"),
    ("布隆过滤器和一致性哈希都用了哈希，用途有什么不同？", "布隆过滤器", "一致性哈希"),
]
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{2,}|\d{2,}")
STOP = {"the", "and", "for", "with", "http", "https", "com", "www", "org", "cnblogs",
        "article", "post", "code", "class", "public", "static", "void", "img", "src",
        "div", "span", "href", "this", "that", "float", "int", "region", "size"}


def psql_rows(sql):
    f = os.path.join(HERE, "_ae.sql")
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
        for esc, real in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(esc, real)
        out.append(line)
    return out


def psql(sql):
    f = os.path.join(HERE, "_ae2.sql")
    io.open(f, "w", encoding="utf-8").write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    return subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                           "-t", "-A", "-f", f], capture_output=True, env=env).stdout.decode("utf-8", "replace")


def embed_all(texts, tag=""):
    out = []
    for i in range(0, len(texts), 16):
        body = json.dumps({"model": EMBED, "input": texts[i:i + 16]}).encode("utf-8")
        req = urllib.request.Request(OLLAMA + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.load(r)["embeddings"])
        if tag and i % 1600 == 0:
            print(f"    嵌入 {tag} {i}/{len(texts)}", flush=True)
    a = np.array(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    rows = psql_rows("SELECT id, doc_id, seq, content FROM chunks ORDER BY doc_id, seq")
    chunks = []
    for line in rows:
        p = line.split("\t", 3)
        if len(p) == 4:
            chunks.append({"id": p[0], "doc": p[1], "seq": int(p[2]), "text": p[3]})
    ids = {c["id"]: i for i, c in enumerate(chunks)}
    print(f"语料 {len(chunks)} 块\n")

    topic_docs = {}
    for t, pats in TOPICS.items():
        s = set()
        for pat in pats:
            s |= {x.strip() for x in psql(
                f"SELECT id FROM documents WHERE name LIKE '{pat}';").splitlines() if x.strip()}
        topic_docs[t] = s
    all_target = {x for s in topic_docs.values() for x in s}

    X = embed_all([c["text"] for c in chunks], "块")

    # 相邻边界信号：depth = (左右各自内部相似度均值) − (跨边界相似度)
    depth = {}
    for i in range(len(chunks)):
        if i == 0 or chunks[i]["doc"] != chunks[i - 1]["doc"]:
            continue
        a = [j for j in range(max(0, i - 2), i)]
        b = [j for j in range(i, min(len(chunks), i + 2))]
        if not a or not b:
            continue
        within = (X[a] @ X[a].T).mean() + (X[b] @ X[b].T).mean()
        cross = (X[a] @ X[b].T).mean()
        depth[i] = within / 2 - cross
    dvals = np.array(list(depth.values())) if depth else np.array([0.0])
    pct = np.percentile(dvals, [10, 25, 50, 75, 90, 99])
    print(f"相邻信号（{len(depth)} 个位置）分位："
          f"p10 {pct[0]:.3f} / p25 {pct[1]:.3f} / 中位 {pct[2]:.3f} / "
          f"p75 {pct[3]:.3f} / p90 {pct[4]:.3f} / p99 {pct[5]:.3f}")
    print(f"  含义：信号低=相邻两块像同一件事（该扩张），高=像换了话题（该停）")
    print(f"  若各分位挤在一起，说明这条信号**分不开** —— 任何阈值都不会好用\n")

    def take(order, budget):
        out, tok = [], 0.0
        for i in order:
            t = len(chunks[i]["text"]) / CPT
            if tok + t > budget:
                continue
            out.append(i)
            tok += t
        return out

    def expand(seed, budget_thr):
        """从 seed 出发沿信号向两侧扩：信号低于阈值（判为「同一件事」）才继续"""
        lo = hi = seed
        while lo - 1 >= 0 and chunks[lo - 1]["doc"] == chunks[seed]["doc"] \
                and depth.get(lo, 0.0) < budget_thr:
            lo -= 1
        while hi + 1 < len(chunks) and chunks[hi + 1]["doc"] == chunks[seed]["doc"] \
                and depth.get(hi + 1, 0.0) < budget_thr:
            hi += 1
        return range(lo, hi + 1)

    cases = [(t, q, None) for t, q in QUESTIONS] + [(None, q, (a, b)) for q, a, b in MULTI]
    for budget in BUDGETS:
        print(f"—— 预算 {budget} ——")
        print(f"{'问题':<26}{'块':>7}{'块+扩张':>10}{'扩张倍率':>10}")
        agg = {"块": [0.0, 0], "扩张": [0.0, 0]}
        ratios = []
        for t, q, pair in cases:
            tgts = (topic_docs[pair[0]] | topic_docs[pair[1]]) if pair else topic_docs[t]
            key = {w for w, ds in
                   ((w, {c["doc"] for c in chunks if w in c["text"].lower()}) for w in set())
                   for _ in ()}
            # 独有词：出现在目标文档、且全体文档频次 ≤3（与粒度对比同一口径）
            v = embed_all([q])[0]
            sim = X @ v
            order = list(np.argsort(-sim))

            sel_plain = take(order, budget)
            # 扩张版：按相似度顺序，遇到未选中的就把它连同相邻段一起收
            chosen, tok = set(), 0.0
            for i in order:
                if i in chosen:
                    continue
                grp = [j for j in expand(i, THR) if j not in chosen]
                gt = sum(len(chunks[j]["text"]) / CPT for j in grp)
                if tok + gt > budget:
                    continue
                chosen.update(grp)
                tok += gt
            ratios.append(len(chosen) / max(len(sel_plain), 1))

            res = {}
            for name, sel in (("块", sel_plain), ("扩张", sorted(chosen))):
                ctx = " ".join(chunks[i]["text"] for i in sel).lower()
                docs = {chunks[i]["doc"] for i in sel}
                hit = (all(bool(docs & topic_docs[x]) for x in pair) if pair
                       else bool(docs & tgts))
                res[name] = hit
                agg[name][1] += int(hit)
            # 覆盖率口径与前面一致：目标文档独有词在上下文里出现多少
            df = {}
            for c in chunks:
                for w in {x.lower() for x in TOKEN.findall(c["text"])} - STOP:
                    df.setdefault(w, set()).add(c["doc"])
            keys = {w for w, ds in df.items() if ds & tgts and len(ds) <= 3}
            for name in ("块", "扩张"):
                sel = sel_plain if name == "块" else sorted(chosen)
                ctx = " ".join(chunks[i]["text"] for i in sel).lower()
                agg[name][0] += sum(1 for w in keys if w in ctx) / max(len(keys), 1)
            print(f"{q[:24]:<26}{100*agg['块'][0]/len(cases):>6.0f}%{100*agg['扩张'][0]/len(cases):>9.0f}%"
                  f"{np.mean(ratios):>9.2f}x")
        n = len(cases)
        print(f"  合计：块 {100*agg['块'][0]/n:.1f}% (命中 {agg['块'][1]}/{n})　"
              f"块+扩张 {100*agg['扩张'][0]/n:.1f}% (命中 {agg['扩张'][1]}/{n})"
              f"　单元数 {np.mean(ratios):.2f}x")
        print()


THR = 0.10


if __name__ == "__main__":
    THR = float(sys.argv[1]) if len(sys.argv) > 1 else 0.10
    main()
