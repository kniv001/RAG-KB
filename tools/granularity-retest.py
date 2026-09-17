# -*- coding: utf-8 -*-
"""
粒度复测（分块改成句子边界之后）。

为什么必须复测：之前那张表（块 84.3% / 句 35.4% / 段 54.0% / 命题 36.1%）是在
**字符硬切**的旧块上量的。而昨天刚把分块改成句子对齐并全量重建了索引，
**块这一侧的基准已经变了** —— 不给新块重排一次，就没法说「段现在还行不行」。

本脚本只比三种：块（新）/ 句（从新块切）/ 段（模型只给边界，逐字原文）。
命题那条路已判负，不再列入。

用法：python tools/granularity-retest.py
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
SQL_FILE = os.path.join(HERE, "_retest.sql")
SEGCACHE = os.path.join(HERE, "..", "data", "_segments_v2.json")
CHAT, EMBED, CPT = "qwen3:4b", "bge-m3", 1.5
BUDGETS = [1500, 4944]
PER_DOC = 3

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
    """与 Java 侧同一套规则：句末标点切；换行两侧有一侧不含中文才切"""
    CJK = re.compile(r"[\u4e00-\u9fff]")
    out, start, line_start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c in "。！？!?；;":
            out.append(text[start:i + 1]); start = i + 1; line_start = i + 1
        elif c == "\n":
            j = text.find("\n", i + 1)
            j = len(text) if j < 0 else j
            if not CJK.search(text[line_start:i]) or not CJK.search(text[i + 1:j]):
                out.append(text[start:i + 1]); start = i + 1
            line_start = i + 1
        i += 1
    if start < len(text):
        out.append(text[start:])
    return [s for s in out if s.strip()] or [text]


def build_segments(chunks):
    if os.path.exists(SEGCACHE):
        segs = json.load(open(SEGCACHE, encoding="utf-8"))
        if segs and segs[0].get("src") == chunks[0]["id"]:
            print(f"复用缓存分段（{len(segs)} 段）")
            return segs
    print("向模型要分段边界…")
    segs, t0, fixed = [], time.time(), 0
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
            fixed += 1
        st = 0
        for x in e:
            piece = "".join(ss[st:x]).strip()
            if piece:
                segs.append({"text": piece, "doc": c["doc"], "src": c["id"]})
            st = x
        if i % 25 == 0:
            print(f"    {i}/{len(chunks)}…", flush=True)
    print(f"  分段完成：{len(segs)} 段，{time.time()-t0:.0f}s，末项被补全的 {fixed} 块")
    json.dump(segs, open(SEGCACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return segs


def pick(sim, items, budget):
    order = np.argsort(-sim)
    used, out, tok, chosen, cur = {}, [], 0.0, set(), 1
    while True:
        added = False
        for i in order:
            i = int(i)
            if i in chosen or used.get(items[i]["src"], 0) >= cur:
                continue
            t = len(items[i]["text"]) / CPT
            if tok + t > budget:
                continue
            chosen.add(i)
            used[items[i]["src"]] = used.get(items[i]["src"], 0) + 1
            out.append(i)
            tok += t
            added = True
        if not added or cur >= 20:
            break
        cur += 1
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    topic_docs = {}
    for t, pats in TOPICS.items():
        ids = set()
        for p in pats:
            ids |= {x.strip() for x in psql(f"SELECT id FROM documents WHERE name LIKE '{p}';").splitlines() if x.strip()}
        topic_docs[t] = ids
        assert ids, t

    ids_sql = ",".join("'" + d + "'" for s in topic_docs.values() for d in s)
    rows = psql(f"SELECT id, doc_id, content FROM ("
                f" SELECT id, doc_id, content, row_number() OVER (PARTITION BY doc_id ORDER BY seq) rn"
                f" FROM chunks WHERE doc_id IN ({ids_sql})) t WHERE rn BETWEEN 2 AND {1+PER_DOC};")
    chunks = []
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            chunks.append({"id": p[0], "doc": p[1], "text": p[2], "src": p[0]})
    sents = [{"text": s.strip(), "doc": c["doc"], "src": c["id"]}
             for c in chunks for s in sentences(c["text"]) if s.strip()]
    segs = build_segments(chunks)
    print(f"料：块 {len(chunks)}（新切分），句 {len(sents)}，段 {len(segs)}")
    print(f"平均 token/条：块 {np.mean([len(c['text']) for c in chunks])/CPT:.0f}，"
          f"句 {np.mean([len(s['text']) for s in sents])/CPT:.0f}，"
          f"段 {np.mean([len(s['text']) for s in segs])/CPT:.0f}\n")

    cv = norm(embed_all([c["text"] for c in chunks], "块"))
    sv = norm(embed_all([s["text"] for s in sents], "句"))
    gv = norm(embed_all([s["text"] for s in segs], "段"))

    df = {}
    for c in chunks:
        for t in {x.lower() for x in TOKEN.findall(c["text"])} - STOP:
            df.setdefault(t, set()).add(c["doc"])

    colls = [("块", chunks, cv), ("句", sents, sv), ("段", segs, gv)]
    cases = [(t, q, None) for t, q in QUESTIONS] + [(None, q, (a, b)) for q, a, b in MULTI]
    for budget in BUDGETS:
        print(f"—— 预算 {budget} token ——")
        agg = {n: [0.0, 0] for n, _, _ in colls}
        for t, q, pair in cases:
            tgts = (set(topic_docs[pair[0]]) | set(topic_docs[pair[1]])) if pair else topic_docs[t]
            key = {w for w, ds in df.items() if ds & tgts and len(ds) <= 3}
            v = norm(embed_all([q]))[0]
            row = []
            for name, items, vec in colls:
                sel = pick(vec @ v, items, budget)
                ctx = " ".join(items[i]["text"] for i in sel).lower()
                cov = sum(1 for w in key if w in ctx) / max(len(key), 1)
                hit = (all(any(items[i]["doc"] in topic_docs[x] for i in sel) for x in pair)
                       if pair else any(items[i]["doc"] in tgts for i in sel))
                agg[name][0] += cov
                agg[name][1] += int(hit)
                row.append(f"{100*cov:>6.0f}%")
            print(f"  {q[:26]:<28}" + "".join(row))
        n = len(cases)
        print("  合计：" + "　".join(
            f"{k} {100*agg[k][0]/n:.1f}% (命中 {agg[k][1]}/{n})" for k, _, _ in colls))
        print()


if __name__ == "__main__":
    main()
