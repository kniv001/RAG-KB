# -*- coding: utf-8 -*-
"""
模型只做切分（不改写）—— 与原句、块、命题一起比。

起因是用户提的改法：命题不该由模型「理解后重写」，让模型**只做切分**。
这正好命中前面测出来的两件事：
  · 命题输给句子，不是因为改写丢词（实测术语保留率 86%），而是因为**改写这一步本身**
    慢（2.9 秒/段）、有 8% 弃抽、且产出的是模型的话而不是原文的话
  · 弃抽的根因是硬切造成的半句片段 —— 那是切分问题，本该由切分解决

只输出边界编号有三个好处，前两个能在本脚本里直接验：
  ① **逐字原文**：编号是索引，段是原文的拼接，不存在改写损失
  ② **失败模式无害**：模型摆烂 → 不切 → 整块作为一个单元，而不是整块丢失
  ③ 输出只有十几个数字，比 250 token 的改写便宜得多

机械校验（这是这个设计最硬的一点）：段拼起来必须逐字等于原文，
编号非法（非递增、末项不等于 N）可以**机械修复**，不需要重试。

用法：python tools/segment-only-probe.py
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
SQL_FILE = os.path.join(HERE, "_seg.sql")
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


def ask_boundaries(sents):
    numbered = "\n".join(f"{i+1}. {s.strip()}" for i, s in enumerate(sents))
    t0 = time.time()
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                           "options": {"temperature": 0.1, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": PROMPT.replace("N", str(len(sents)))},
                                        {"role": "user", "content": numbered}]})
    ms = int((time.time() - t0) * 1000)
    txt = r.get("message", {}).get("content", "")
    try:
        ends = json.loads(txt).get("ends", [])
    except Exception:
        m = re.search(r'"ends"\s*:\s*\[([^\]]*)\]', txt)
        ends = [int(x) for x in re.findall(r"\d+", m.group(1))] if m else []
    return ends, ms


def repair(ends, n):
    """机械修复：排序去重、夹到 [1,n]、末项补 n。返回 (ends, 是否修过)"""
    raw = list(ends)
    e = sorted({x for x in ends if isinstance(x, int) and 1 <= x <= n})
    if not e or e[-1] != n:
        e = e + [n]
    fixed = e != raw or (raw and raw[-1] != n)
    return e, fixed


def embed_all(texts, tag=""):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]})["embeddings"])
        if tag and i % 800 == 0:
            print(f"    嵌入 {tag} {i}/{len(texts)}", flush=True)
    return np.array(out, dtype=np.float32)


def norm(a):
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def pick(sim, items, budget):
    order = np.argsort(-sim)
    used, out, tok, chosen = {}, [], 0.0, set()
    cur = 1
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

    recs = json.load(open(os.path.join(HERE, "..", "data", "_prop-probe.json"),
                          encoding="utf-8"))["records"]
    chunks = [{"id": r["chunk"], "doc": r["doc"], "text": r["text"], "src": r["chunk"]}
              for r in recs]
    facts = [{"text": f["text"], "doc": f["doc"], "src": f["chunk"]}
             for f in json.load(open(os.path.join(HERE, "..", "data", "_prop-fixed.json"),
                                     encoding="utf-8"))["facts"]]

    print("—— 模型切分（只输出边界编号）——")
    segs, n_fix, n_bad, tot_ms = [], 0, 0, 0
    exact = 0
    for i, c in enumerate(chunks, 1):
        ss = sentences(c["text"])
        if len(ss) < 3:
            segs.append({"text": c["text"], "doc": c["doc"], "src": c["id"]})
            continue
        ends, ms = ask_boundaries(ss)
        tot_ms += ms
        e, fixed = repair(ends, len(ss))
        if fixed:
            n_fix += 1
        if not ends:
            n_bad += 1
        start = 0
        pieces = []
        for x in e:
            pieces.append("".join(ss[start:x]))
            start = x
        if "".join(pieces) == "".join(ss):
            exact += 1
        for p in pieces:
            if p.strip():
                segs.append({"text": p.strip(), "doc": c["doc"], "src": c["id"]})
        if i % 20 == 0:
            print(f"    {i}/{len(chunks)}…", flush=True)

    sents = [{"text": s.strip(), "doc": c["doc"], "src": c["id"]}
             for c in chunks for s in sentences(c["text"]) if s.strip()]
    print(f"\n段数 {len(segs)}（源块 {len(chunks)}，平均 {len(segs)/len(chunks):.1f} 段/块）")
    print(f"边界非法（非递增/越界/末项≠N）被机械修复：{n_fix}/{len(chunks)}")
    print(f"完全没给出编号（摆烂 → 不切 → 整块成一段）：{n_bad}/{len(chunks)}")
    print(f"拼接后逐字等于原文：{exact}/{len(chunks)}")
    print(f"平均耗时 {tot_ms/len(chunks):.0f} ms/块")
    print(f"平均 token/条：段 {np.mean([len(s['text']) for s in segs])/CPT:.0f}，"
          f"句 {np.mean([len(s['text']) for s in sents])/CPT:.0f}，"
          f"块 {np.mean([len(c['text']) for c in chunks])/CPT:.0f}\n")

    topic_docs = {}
    for t, pats in TOPICS.items():
        ids = set()
        for p in pats:
            ids |= {x.strip() for x in psql(f"SELECT id FROM documents WHERE name LIKE '{p}';").splitlines() if x.strip()}
        topic_docs[t] = ids
        assert ids, t

    cv = norm(embed_all([c["text"] for c in chunks]))
    sv = norm(embed_all([s["text"] for s in sents], "句"))
    gv = norm(embed_all([s["text"] for s in segs], "段"))
    fv = norm(embed_all([f["text"] for f in facts], "命题"))

    df = {}
    for c in chunks:
        for t in {x.lower() for x in TOKEN.findall(c["text"])} - STOP:
            df.setdefault(t, set()).add(c["doc"])

    colls = [("块", chunks, cv), ("句", sents, sv), ("段(模型切)", segs, gv), ("命题", facts, fv)]
    cases = [(t, q, None) for t, q in QUESTIONS] + [(None, q, (a, b)) for q, a, b in MULTI]
    for budget in BUDGETS:
        print(f"—— 预算 {budget} token（独有词覆盖 / 命中）——")
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
