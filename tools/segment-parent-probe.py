# -*- coding: utf-8 -*-
"""
段检索 + 父块注入：能不能同时拿到「段的命中率」与「块的密度」。

已测的两个端点：
  · 块：密度最高（84.3% @4944），但紧预算会漏（1500 时命中 8/10）
  · 段：紧预算不漏（10/10），但密度低（54.0% @4944）

这两条短板指向同一个组合 —— **用段做检索键（匹配更准），命中后注入它所属的父块（内容足）**。
它相当于「用最好的那一段去给块排序」，所以排序质量应该不差于块级相似度，
而送进提示词的仍是整块原文。

三路并排：
  块       —— 直接用块相似度选块（现状）
  段       —— 选段、送段（上一轮已测）
  段→块    —— 按「该块最好的那一段」的相似度给块排序，选块、送块

用法：python tools/segment-parent-probe.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(HERE, "_sp.sql")
SEGCACHE = os.path.join(HERE, "..", "data", "_segments.json")
CHAT, EMBED, CPT = "qwen3:4b", "bge-m3", 1.5
BUDGETS = [1500, 4944]

PROMPT = """下面这段资料已按句子编号（1 到 N）。请按**语义**把它切成若干段，每段讲一件事。

只输出每段的**结束句编号**，JSON：{"ends":[3,7,12]}

要求：
- 编号从 1 开始、严格递增，**最后一项必须等于 N**
- 每段 2~8 句；不要把整段当成一段，也不要逐句切
- 不要改写、不要解释"""
SCHEMA = {"type": "object",
          "properties": {"ends": {"type": "array", "items": {"type": "integer"}}},
          "required": ["ends"]}

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
}
QUESTIONS = [
    ("HTTP/2", "HTTP/2 的多路复用是怎么实现的？"),
    ("Redis 持久化", "Redis 的 RDB 和 AOF 有什么区别？"),
    ("布隆过滤器", "布隆过滤器为什么会有误判，误判率怎么算？"),
    ("向量数据库", "HNSW 的 efSearch 参数控制什么？"),
    ("B-tree", "B 树的索引页里都有什么？"),
    ("Kubernetes", "Pod 调度里的亲和性是怎么配置的？"),
    ("模型量化", "int8 量化和 4bit 量化的区别是什么？"),
    ("Docker", "Docker 的 bridge 网络是怎么连通的？"),
]
MULTI = [
    ("Redis 的 AOF 和 Kafka 的日志复制在持久化思路上有什么不同？", "Redis 持久化", "Kafka"),
    ("布隆过滤器和一致性哈希都用了哈希，用途有什么不同？", "布隆过滤器", "一致性哈希"),
]
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{2,}|\d{2,}")
STOP = {"the", "and", "for", "with", "http", "https", "com", "www", "org", "cnblogs",
        "article", "post", "code", "class", "public", "static", "void", "img", "src",
        "div", "span", "href", "this", "that", "float", "int"}


def psql(sql):
    with open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE], capture_output=True, env=env)
    return p.stdout.decode("utf-8", "replace")


def post(path, body, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def embed_all(texts, tag=""):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]})["embeddings"])
        if tag and i % 800 == 0:
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
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return out


def build_segments(chunks):
    """段边界由模型给，段本身是原文拼接；结果缓存到 data/_segments.json"""
    if os.path.exists(SEGCACHE):
        segs = json.load(open(SEGCACHE, encoding="utf-8"))
        if len(segs) and segs[0].get("src") == chunks[0]["id"]:
            print(f"复用缓存的分段（{len(segs)} 段）")
            return segs
    print("向模型要分段边界…")
    segs, t0 = [], time.time()
    for i, c in enumerate(chunks, 1):
        ss = sentences(c["text"])
        if len(ss) < 3:
            segs.append({"text": c["text"], "doc": c["doc"], "src": c["id"]})
            continue
        numbered = "\n".join(f"{j+1}. {s.strip()}" for j, s in enumerate(ss))
        r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                               "options": {"temperature": 0.1, "num_ctx": 8192},
                               "messages": [{"role": "system",
                                             "content": PROMPT.replace("N", str(len(ss)))},
                                            {"role": "user", "content": numbered}]})
        txt = r.get("message", {}).get("content", "")
        try:
            ends = json.loads(txt).get("ends", [])
        except Exception:
            m = re.search(r'"ends"\s*:\s*\[([^\]]*)\]', txt)
            ends = [int(x) for x in re.findall(r"\d+", m.group(1))] if m else []
        e = sorted({x for x in ends if isinstance(x, int) and 1 <= x <= len(ss)}) or [len(ss)]
        if e[-1] != len(ss):
            e.append(len(ss))
        start = 0
        for x in e:
            piece = "".join(ss[start:x]).strip()
            if piece:
                segs.append({"text": piece, "doc": c["doc"], "src": c["id"]})
            start = x
        if i % 25 == 0:
            print(f"    {i}/{len(chunks)}…", flush=True)
    print(f"  分段完成：{len(segs)} 段，{time.time()-t0:.0f}s")
    json.dump(segs, open(SEGCACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return segs


def take(order, tok_of, budget):
    """按给定顺序装到预算用尽"""
    out, tok = [], 0.0
    for i in order:
        t = tok_of(i)
        if tok + t > budget:
            continue
        out.append(i)   # 不强转：块 id 是字符串，段/块下标是 np.int64，各自能用就行
        tok += t
    return out, tok


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    recs = json.load(open(os.path.join(HERE, "..", "data", "_prop-probe.json"),
                          encoding="utf-8"))["records"]
    chunks = [{"id": r["chunk"], "doc": r["doc"], "text": r["text"]} for r in recs]
    segs = build_segments(chunks)
    print(f"料：块 {len(chunks)}，段 {len(segs)}\n")

    cv = norm(embed_all([c["text"] for c in chunks], "块"))
    gv = norm(embed_all([s["text"] for s in segs], "段"))

    topic_docs = {}
    for t, pats in TOPICS.items():
        ids = set()
        for p in pats:
            ids |= {x.strip() for x in psql(f"SELECT id FROM documents WHERE name LIKE '{p}';").splitlines() if x.strip()}
        topic_docs[t] = ids
        assert ids, t

    df = {}
    for c in chunks:
        for t in {x.lower() for x in TOKEN.findall(c["text"])} - STOP:
            df.setdefault(t, set()).add(c["doc"])

    seg_of_chunk = {}
    for i, s in enumerate(segs):
        seg_of_chunk.setdefault(s["src"], []).append(i)
    chunk_of_seg = [s["src"] for s in segs]
    cidx = {c["id"]: i for i, c in enumerate(chunks)}

    def order_chunks_by_segment(sim_s):
        """按「该块最好的那一段」的相似度给块排序"""
        best = {}
        for i, s in enumerate(sim_s):
            cid = chunk_of_seg[i]
            if cid not in best or s > best[cid]:
                best[cid] = s
        return sorted(best, key=lambda cid: -best[cid]), lambda cid: len(chunks[cidx[cid]]["text"]) / CPT

    def order_segments(sim_s, cap=999):
        order, used = [], {}
        for i in np.argsort(-sim_s):
            i = int(i)
            if used.get(chunk_of_seg[i], 0) >= cap:
                continue
            used[chunk_of_seg[i]] = used.get(chunk_of_seg[i], 0) + 1
            order.append(i)
        return order, lambda i: len(segs[i]["text"]) / CPT

    cases = [(t, q, None) for t, q in QUESTIONS] + [(None, q, (a, b)) for q, a, b in MULTI]
    for budget in BUDGETS:
        print(f"—— 预算 {budget} token ——")
        print(f"{'问题':<28}{'块':>8}{'段':>8}{'段→块':>9}")
        agg = {"块": [0.0, 0], "段": [0.0, 0], "段→块": [0.0, 0]}
        for t, q, pair in cases:
            tgts = (set(topic_docs[pair[0]]) | set(topic_docs[pair[1]])) if pair else topic_docs[t]
            key = {w for w, ds in df.items() if ds & tgts and len(ds) <= 3}
            v = norm(embed_all([q]))[0]
            cs, gs = cv @ v, gv @ v
            o1, tok1 = take(np.argsort(-cs), lambda i: len(chunks[i]["text"]) / CPT, budget)
            o2, _ = order_segments(gs, cap=1)
            o2, tok2 = take(o2, lambda i: len(segs[i]["text"]) / CPT, budget)
            o3, tok3 = order_chunks_by_segment(gs)
            o3, _ = take(o3, lambda cid: len(chunks[cidx[cid]]["text"]) / CPT, budget)
            o3 = [cidx[c] for c in o3]
            res = {}
            for name, idxs, docs in (("块", o1, [chunks[i]["doc"] for i in o1]),
                                     ("段", o2, [segs[i]["doc"] for i in o2]),
                                     ("段→块", o3, [chunks[i]["doc"] for i in o3])):
                ctx = " ".join((chunks if name != "段" else segs)[i]["text"] for i in idxs).lower()
                cov = sum(1 for w in key if w in ctx) / max(len(key), 1)
                hit = (all(any(d in topic_docs[x] for d in docs) for x in pair)
                       if pair else any(d in tgts for d in docs))
                res[name] = (cov, hit)
                agg[name][0] += cov
                agg[name][1] += int(hit)
            print(f"  {q[:26]:<28}" + "".join(f"{100*res[n][0]:>7.0f}%" for n in ("块", "段", "段→块")))
        n = len(cases)
        print("  合计：" + "　".join(
            f"{k} {100*agg[k][0]/n:.1f}% (命中 {agg[k][1]}/{n})" for k in ("块", "段", "段→块")))
        print()


if __name__ == "__main__":
    main()
