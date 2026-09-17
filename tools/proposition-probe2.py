# -*- coding: utf-8 -*-
"""
命题索引 A/B（修正版）+ 0 命题块的定性。

修正了什么：上一版的目标文档映射取自入库清单，而第一轮入库的文档没有主题标注
（kafka 那篇就是），于是「Redis vs Kafka」那一跳的目标集是**空集**，
判定必然为失败 —— 那个 4/6 不可用。这一版改成按文档名匹配，并对每个目标集加断言。

同时回答上一版留下的疑点：47% 的块抽出 0 条命题。两种可能后果完全不同 ——
  · 那些块本来就是导航/版权/URL 之类的垃圾 → 抽取器**正确地**拒绝，是加分项
  · 那些块是正文 → 抽取器**静默失败**，整个方案的地基有问题
所以把 0 命题块的原始输出打出来看。

用法：python tools/proposition-probe2.py
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
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_p2.sql")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_prop-probe.json")

CHAT = "qwen3:4b"
EMBED = "bge-m3"
PER_DOC = 3
BUDGET_TOKENS = 4944
CPT = 1.5
TOP_K = 12

PROMPT = """把下面这段资料拆成若干条**自足的事实命题**。

硬性要求：
1. 每条命题必须脱离上下文就能读懂 —— 不许出现「它」「该」「此」「上述」「前者」这类指代，
   指代对象要写出来。
2. 只写资料里说过的事实。**不许补充资料之外的任何内容，不许推论**。
   资料里没有的数字、结论、因果关系一律不得出现。
3. 一条命题只讲一件事。同一条事实被反复说，只保留一次。
4. 保持原有术语与数字，不要改写数值。

输出 JSON：{"facts":["命题一","命题二"]}"""

# 注意：这里**不能**写「若没有事实就返回空数组」这类逃生口。
# 实测代价：加了那一句之后，74 段里 72 段返回空数组，而且每次只花 200~300ms
# —— 模型不再读资料，直接走逃生口。抽取率从 7.7 条/块塌到 0.1 条/块。
# 结论：4B 对提示词里这类「允许不做」的口子是照单全收的。

SCHEMA = {"type": "object",
          "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
          "required": ["facts"]}

# 主题 → 文档名的 LIKE 模式（按名字找，不依赖清单标注）
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
PRONOUN = re.compile(r"(它|该|此|其|上述|前者|后者|本文|本段|这种|这些)")
NUM = re.compile(r"\d+(?:\.\d+)?")


def psql(sql):
    with open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE],
                       capture_output=True, env=env)
    if p.returncode != 0:
        print("psql 失败:", p.stderr.decode("utf-8", "backslashreplace")[:400])
        sys.exit(1)
    return p.stdout.decode("utf-8", "replace")


def post(path, body, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def extract(chunk):
    t0 = time.time()
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False,
                           "format": SCHEMA,
                           "options": {"temperature": 0.1, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": PROMPT},
                                        {"role": "user", "content": chunk}]})
    raw = r.get("message", {}).get("content", "")
    ms = int((time.time() - t0) * 1000)
    facts = []
    try:
        facts = json.loads(raw).get("facts", [])
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                facts = json.loads(m.group(0)).get("facts", [])
            except Exception:
                pass
    return facts, raw, ms


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

    # ---- 目标映射（按名字，带断言）----
    topic_docs, bad = {}, []
    for t, pats in TOPICS.items():
        ids = []
        for p in pats:
            s = psql(f"SELECT id FROM documents WHERE name LIKE '{p}';")
            ids += [x.strip() for x in s.splitlines() if x.strip()]
        topic_docs[t] = sorted(set(ids))
        if not topic_docs[t]:
            bad.append(t)
    if bad:
        print("❌ 目标集为空，实验无效：", bad)
        sys.exit(1)
    print("目标文档：")
    for t, ids in topic_docs.items():
        print(f"  {t:<14} {len(ids)} 篇")
    all_docs = sorted({d for ids in topic_docs.values() for d in ids})

    # ---- 取样 ----
    ids_sql = ",".join("'" + d + "'" for d in all_docs)
    rows = psql(f"SELECT id, doc_id, content FROM ("
                f" SELECT id, doc_id, content, row_number() OVER (PARTITION BY doc_id ORDER BY seq) rn"
                f" FROM chunks WHERE doc_id IN ({ids_sql})) t"
                f" WHERE rn BETWEEN 2 AND {1 + PER_DOC};")
    chunks = []
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            chunks.append({"id": p[0], "doc": p[1], "text": p[2]})
    print(f"\n取样 {len(chunks)} 段（{len(all_docs)} 篇文档，每篇最多 {PER_DOC} 段）")

    # ---- 抽取 ----
    recs, facts = [], []
    for i, ch in enumerate(chunks, 1):
        fs, raw, ms = extract(ch["text"])
        recs.append({"chunk": ch["id"], "doc": ch["doc"], "text": ch["text"],
                     "facts": fs, "raw": raw, "ms": ms})
        for f in fs:
            facts.append({"text": f, "doc": ch["doc"], "chunk": ch["id"]})
        flag = "" if fs else "  ← 0 条"
        print(f"  [{i:>2}/{len(chunks)}] {len(fs):>2} 条 {ms:>5}ms {ch['text'][:28]}…{flag}", flush=True)
    json.dump({"records": recs}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---- 定性 0 命题块 ----
    empty = [r for r in recs if not r["facts"]]
    print(f"\n—— 0 命题块 {len(empty)}/{len(recs)} ——")
    for r in empty[:6]:
        print(f"  原文: {r['text'][:80]!r}")
        print(f"  输出: {r['raw'][:120]!r}")
    junk = sum(1 for r in empty if len(r["text"]) < 120 or
               re.match(r"^\s*(https?://|/|[\w\-\.]+\.(com|cn|net))", r["text"]))
    print(f"  其中明显是垃圾块（<120 字或网址/导航样式）：{junk}/{len(empty)}")

    # ---- 质量 ----
    nf = len(facts)
    if nf == 0:
        print("没有抽到任何命题，后续跳过")
        return
    src = [r["text"] for r in recs if r["facts"]]
    n_pron = sum(1 for f in facts if PRONOUN.search(f["text"]))
    n_hall = 0
    by_chunk = {r["chunk"]: r["text"] for r in recs}
    for f in facts:
        if any(x not in set(NUM.findall(by_chunk[f["chunk"]])) for x in NUM.findall(f["text"])):
            n_hall += 1
    # 退化检测：响应过快 + 空结果 = 模型没读资料就走了逃生口，不是「正确地判为无内容」
    degen = sum(1 for r in recs if not r["facts"] and r["ms"] < 400)
    print(f"疑似退化（<400ms 且空）：{degen}/{len(recs)}")
    flen = [len(f["text"]) for f in facts]
    print(f"\n—— 抽取质量 ——")
    print(f"命题 {nf} 条，平均 {nf/len(recs):.1f} 条/块（有命题的块 {len(src)}）")
    print(f"命题均值 {np.mean(flen):.0f} 字；有命题块的块均值 "
          f"{np.mean([len(s) for s in src]):.0f} 字")
    print(f"指代残留 {n_pron}/{nf} = {100*n_pron/nf:.1f}%")
    print(f"数字越界 {n_hall}/{nf} = {100*n_hall/nf:.1f}%")

    # ---- A/B ----
    cvec, fvec = norm(embed_all([c["text"] for c in chunks])), norm(embed_all([f["text"] for f in facts]))

    def top(q, k=TOP_K):
        v = norm(embed_all([q]))[0]
        ci = np.argsort(-(cvec @ v))[:k]
        fi = np.argsort(-(fvec @ v))[:k]
        return ([chunks[i]["doc"] for i in ci], [facts[i]["doc"] for i in fi],
                len({chunks[i]["id"] for i in ci}), len({facts[i]["chunk"] for i in fi}))

    print(f"\n—— 检索 A/B（top-{TOP_K}）——")
    print(f"{'问题':<34}{'块索引':>9}{'命题索引':>9}{'块覆盖源块':>11}{'命题覆盖源块':>13}")
    sc = sf = 0
    for t, q in SINGLE:
        tgt = set(topic_docs[t])
        cd, fd, cc, fc = top(q)
        a, b = bool(tgt & set(cd)), bool(tgt & set(fd))
        sc += a
        sf += b
        print(f"{q[:32]:<34}{('✅' if a else '❌'):>9}{('✅' if b else '❌'):>9}{cc:>11}{fc:>13}")
    mc = mf = 0
    for q, t1, t2 in MULTI:
        d1, d2 = set(topic_docs[t1]), set(topic_docs[t2])
        cd, fd, cc, fc = top(q)
        a = bool(d1 & set(cd)) and bool(d2 & set(cd))
        b = bool(d1 & set(fd)) and bool(d2 & set(fd))
        mc += a
        mf += b
        print(f"{q[:32]:<34}{('✅' if a else '❌'):>9}{('✅' if b else '❌'):>9}{cc:>11}{fc:>13}")
    print(f"\n单跳命中　块 {sc}/{len(SINGLE)}　命题 {sf}/{len(SINGLE)}")
    print(f"多跳两跳都中　块 {mc}/{len(MULTI)}　命题 {mf}/{len(MULTI)}")
    print("（覆盖源块 = top-12 落在多少个**不同源块**上；命题索引若集中在一两个块上，"
          "说明它挤占了预算却没换来广度）")


if __name__ == "__main__":
    main()
