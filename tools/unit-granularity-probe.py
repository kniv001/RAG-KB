# -*- coding: utf-8 -*-
"""
粒度对比：块 / 句子 / 命题，在**等 token 预算**下谁把更多「该文档的独有内容」装进上下文。

为什么不能只看命中率：上一轮的题太容易，三种粒度都 9/9 打平，区分不出来。
命中有个天花板，而真正的区别在**同样预算下装进去的是不是有用的东西**。

所以判据换成「答案素材命中率」，纯机械、无模型在环路里：
  · 对每道题，先从**目标文档在该批取样里的那些段**中挑出「独有词」
    （英文标识符、参数名、数字 —— 只在少数文档里出现的那些）
  · 再看各粒度在**同一个 token 预算**下选出的上下文里，这些独有词出现了多少
  · 另有「覆盖了多少个不同源块」与「用掉多少 token」两个观测量

关键对照是**等预算**而不是等条数：12 段块 ≈ 4020 token，同样的预算能放
约 200 条命题（每源块限 1 条）—— 广度与深度谁更值，这一支就是来回答这个的。

用法：python tools/unit-granularity-probe.py
"""
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
SQL_FILE = os.path.join(HERE, "_gran.sql")
EMBED = "bge-m3"
CPT = 1.5
BUDGETS = [1500, 4944]   # 紧（长对话）/ 松（短对话）

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

# 常见词不算「独有内容」
STOP = {"the", "and", "for", "with", "http", "https", "com", "www", "org",
        "cnblogs", "article", "post", "code", "class", "public", "static", "void",
        "img", "src", "div", "span", "href", "this", "that", "float", "int"}


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


def embed_all(texts):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]},
                        timeout=300)["embeddings"])
    return np.array(out, dtype=np.float32)


def norm(a):
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def sentences(text, lo=12, hi=300):
    """按中英文句末标点切句，过短的并入下一句"""
    parts = re.split(r"(?<=[。！？!?；;\n])\s*", text)
    out, buf = [], ""
    for p in parts:
        buf += p
        if len(buf) >= lo:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    recs = json.load(open(os.path.join(HERE, "..", "data", "_prop-probe.json"),
                          encoding="utf-8"))["records"]
    chunks = [{"id": r["chunk"], "doc": r["doc"], "text": r["text"], "src": r["chunk"]}
              for r in recs]
    facts_raw = json.load(open(os.path.join(HERE, "..", "data", "_prop-fixed.json"),
                               encoding="utf-8"))["facts"]
    facts = [{"text": f["text"], "doc": f["doc"], "chunk": f["chunk"], "src": f["chunk"]}
             for f in facts_raw]
    sents = []
    for c in chunks:
        for s in sentences(c["text"]):
            sents.append({"text": s, "doc": c["doc"], "chunk": c["id"], "src": c["id"]})
    print(f"料：块 {len(chunks)}，句 {len(sents)}，命题 {len(facts)}")
    print(f"平均 token/条：块 {np.mean([len(c['text']) for c in chunks])/CPT:.0f}，"
          f"句 {np.mean([len(s['text']) for s in sents])/CPT:.0f}，"
          f"命题 {np.mean([len(f['text']) for f in facts])/CPT:.0f}\n")

    topic_docs = {}
    for t, pats in TOPICS.items():
        ids = set()
        for p in pats:
            ids |= {x.strip() for x in psql(f"SELECT id FROM documents WHERE name LIKE '{p}';").splitlines() if x.strip()}
        topic_docs[t] = ids
        assert ids, t

    cv, sv, fv = (norm(embed_all([x["text"] for x in coll]))
                  for coll in (chunks, sents, facts))

    def select(vec, items, sim, budget, cap):
        """
        按相似度降序装，每源块最多 cap 条（cap=0 不限），直到预算用尽。

        cap=0 时是「一次装到底」；cap>0 时要**渐进放宽**：先用每块 1 条铺满广度，
        预算还有剩就放宽到 2 条、3 条…… 否则像上一版那样，68 个源块 × 1 条
        只用掉 1156 token，预算还剩四分之三 —— 量出来的不是这种粒度的上限，
        是我的规则太紧。
        """
        order = np.argsort(-sim)
        used, out, tok, chosen = {}, [], 0.0, set()
        cur = cap or 999
        while True:
            added = False
            for i in order:
                i = int(i)
                if i in chosen:
                    continue
                it = items[i]
                if used.get(it["src"], 0) >= cur:
                    continue
                t = len(it["text"]) / CPT
                if tok + t > budget:
                    continue
                chosen.add(i)
                used[it["src"]] = used.get(it["src"], 0) + 1
                out.append(i)
                tok += t
                added = True
            if not cap or not added or cur >= 20:
                break
            cur += 1
        return out, tok

    colls = {"块": (chunks, cv), "句≤1": (sents, sv), "句∞": (sents, sv),
             "命题≤1": (facts, fv), "命题∞": (facts, fv)}
    caps = {"块": 0, "句≤1": 1, "句∞": 0, "命题≤1": 1, "命题∞": 0}

    # 独有词：只出现在少数文档里的英文标识符/数字
    df = {}
    for c in chunks:
        for t in {x.lower() for x in TOKEN.findall(c["text"])} - STOP:
            df.setdefault(t, set()).add(c["doc"])

    cases = [(t, q, None) for t, q in QUESTIONS] + [(None, q, (a, b)) for q, a, b in MULTI]
    for budget in BUDGETS:
        print(f"—— 预算 {budget} token ——")
        print(f"{'问题':<30}" + "".join(f"{k:>10}" for k in colls))
        agg = {k: [0.0, 0, 0] for k in colls}
        for t, q, pair in cases:
            tgts = set(topic_docs[pair[0]]) | set(topic_docs[pair[1]]) if pair else topic_docs[t]
            # 该题的目标独有词：出现在目标文档、且全体文档频次 ≤3 的
            key = {w for w, docs in df.items() if docs & tgts and len(docs) <= 3}
            v = norm(embed_all([q]))[0]
            row = []
            for name, (items, vec) in colls.items():
                idx, tok = select(vec, items, vec @ v, budget, caps[name])
                ctx = " ".join(items[i]["text"] for i in idx).lower()
                cov = sum(1 for w in key if w in ctx) / max(len(key), 1)
                ndoc = len({items[i]["doc"] for i in idx if items[i]["doc"] in tgts})
                hit = ndoc > 0 if not pair else all(
                    any(items[i]["doc"] in topic_docs[x] for i in idx) for x in pair)
                agg[name][0] += cov
                agg[name][1] += int(hit)
                agg[name][2] += tok
                row.append(f"{100*cov:>9.0f}%")
            print(f"{q[:28]:<30}" + "".join(row))
        n = len(cases)
        print(f"{'独有词覆盖 / 命中 / 用掉预算':<30}")
        for k in colls:
            c, h, s = agg[k]
            print(f"  {k:<8} 独有词 {100*c/n:>5.1f}%　命中 {h}/{n}　"
                  f"平均用掉 {s/n:>5.0f}/{budget} token")
        print()


if __name__ == "__main__":
    main()
