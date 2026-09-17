# -*- coding: utf-8 -*-
"""
命题索引的「扎堆」问题：按源块限流之后，能不能反超块索引。

上一支探针发现：命题索引的 top-12 只落在 1~6 个不同源块上，而块索引稳定覆盖 12 个。
原因是同一块内有十几条近义命题，相似度排序让它们互相挤占预算 —— 单元越碎，
top-k 越容易扎堆。这不是实现偶然，是小单元索引的固有性质。

所以本探针只在**选取规则**上做文章（不动已抽好的命题）：
  · 无限流（现状）
  · 每源块最多取 n 条（n = 1/2/3）
比较覆盖面与两跳命中率。若限流后仍不反超，命题层的价值就要重新掂量。

用法：python tools/prop-diversity-probe.py
"""
import json
import os
import subprocess
import sys
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_div.sql")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_prop-probe.json")
EMBED, TOP_K = "bge-m3", 12

TOPICS = {
    "HTTP/2": ["%HTTP 2%", "%HTTP2%", "%http系列%"],
    "Redis 持久化": ["%Redis持久化%", "%Redis两种持久化%"],
    "Kafka 分区": ["%kafka消费组%"],
    "Docker 网络": ["%docker容器网络%"],
    "Kubernetes 调度": ["%Kubernetes Pod调度%"],
    "布隆过滤器": ["%布隆过滤器%"],
    "一致性哈希": ["%一致性哈希%"],
    "向量数据库": ["%向量数据库%", "%HNSW%"],
    "模型量化": ["%模型量化%", "%INT8量化%"],
    "B-tree": ["%B-tree 索引结构%"],
}
SINGLE = [("HTTP/2", "HTTP/2 的多路复用是怎么回事？"),
          ("Redis 持久化", "Redis 的 RDB 和 AOF 有什么区别？"),
          ("布隆过滤器", "布隆过滤器为什么会有误判？"),
          ("向量数据库", "向量数据库是怎么做检索的？"),
          ("B-tree", "B 树的索引页里都有什么？")]
MULTI = [("Redis 的 AOF 和 Kafka 的日志复制在持久化思路上有什么不同？", "Redis 持久化", "Kafka 分区"),
         ("布隆过滤器和一致性哈希都用了哈希，用途有什么不同？", "布隆过滤器", "一致性哈希"),
         ("Docker 的 namespace 和 Kubernetes 的 Pod 调度是什么关系？", "Docker 网络", "Kubernetes 调度"),
         ("模型量化对向量检索的精度有什么影响？", "模型量化", "向量数据库")]


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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    recs = json.load(open(DATA, encoding="utf-8"))["records"]
    chunks = [{"id": r["chunk"], "doc": r["doc"], "text": r["text"]} for r in recs]
    facts = [{"text": f, "doc": r["doc"], "chunk": r["chunk"]}
             for r in recs for f in r["facts"]]
    print(f"复用已抽好的料：源块 {len(chunks)}，命题 {len(facts)}"
          f"（来自 {len({f['chunk'] for f in facts})} 个有命题的块）\n")

    topic_docs = {}
    for t, pats in TOPICS.items():
        ids = set()
        for p in pats:
            ids |= {x.strip() for x in psql(f"SELECT id FROM documents WHERE name LIKE '{p}';").splitlines() if x.strip()}
        topic_docs[t] = ids
        assert ids, f"目标集为空：{t}"

    cvec, fvec = norm(embed_all([c["text"] for c in chunks])), norm(embed_all([f["text"] for f in facts]))
    facts_chunk = [f["chunk"] for f in facts]
    facts_doc = [f["doc"] for f in facts]

    def pick_facts(sim, cap):
        """按相似度降序，每个源块最多取 cap 条"""
        order = np.argsort(-sim)
        used, out = {}, []
        for i in order:
            c = facts_chunk[i]
            if cap and used.get(c, 0) >= cap:
                continue
            used[c] = used.get(c, 0) + 1
            out.append(int(i))
            if len(out) >= TOP_K:
                break
        return out

    print(f"{'问题':<34}" + "".join(f"{lbl:>11}" for lbl in
          ["块索引", "命题∞", "命题≤3", "命题≤2", "命题≤1"]))
    tot = {"c": 0, "f0": 0, "f3": 0, "f2": 0, "f1": 0}
    cov = {k: [] for k in ("c", "f0", "f3", "f2", "f1")}
    cases = [(t, q, None) for t, q in SINGLE] + [(None, q, (t1, t2)) for q, t1, t2 in MULTI]
    n = len(cases)
    for t, q, pair in cases:
        v = norm(embed_all([q]))[0]
        cs, fs = cvec @ v, fvec @ v
        ci = list(np.argsort(-cs)[:TOP_K])
        picks = {"c": ci,
                 "f0": list(np.argsort(-fs)[:TOP_K]),
                 "f3": pick_facts(fs, 3), "f2": pick_facts(fs, 2), "f1": pick_facts(fs, 1)}
        if pair:
            tgts = [topic_docs[pair[0]], topic_docs[pair[1]]]
            good = {k: all(any(facts_doc[i] in tt for i in idx) for tt in tgts)
                    for k, idx in picks.items() if k != "c"}
            good["c"] = all(any(chunks[i]["doc"] in tt for i in ci) for tt in tgts)
        else:
            tgt = topic_docs[t]
            good = {k: any(facts_doc[i] in tgt for i in idx) for k, idx in picks.items() if k != "c"}
            good["c"] = any(chunks[i]["doc"] in tgt for i in ci)
        for k in tot:
            tot[k] += good[k]
            idx = picks[k]
            cov[k].append(len({chunks[i]["id"] for i in idx}) if k == "c"
                          else len({facts_chunk[i] for i in idx}))
        print(f"{q[:32]:<34}" + "".join(f"{('✅' if good[k] else '❌'):>11}" for k in
                                        ("c", "f0", "f3", "f2", "f1")))
    print(f"\n命中 {n} 题中：" + "  ".join(f"{lbl} {tot[k]}" for k, lbl in
          [("c", "块"), ("f0", "命题∞"), ("f3", "命题≤3"), ("f2", "命题≤2"), ("f1", "命题≤1")]))
    print("top-12 平均覆盖的不同源块数：" + "  ".join(
        f"{lbl} {np.mean(cov[k]):.1f}" for k, lbl in
        [("c", "块"), ("f0", "命题∞"), ("f3", "命题≤3"), ("f2", "命题≤2"), ("f1", "命题≤1")]))


if __name__ == "__main__":
    main()
