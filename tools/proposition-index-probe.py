# -*- coding: utf-8 -*-
"""
命题抽取质量 + 命题索引 vs 块索引的 A/B。

要验的断言（用户提出）：「4B 模型对于信息的切分应该还是可以的」。
这句话是整个重建方案的地基 —— 抽坏了，上面的树和节点都建在坏料上。

所以分两段量，第二段才是关键：
  ① 抽取质量（静态）：每块出几条、有没有残留指代、有没有引入原文没有的数字
  ② 功能 A/B（动态）：同一批源料，建两个索引（块 / 命题），
     在同一批问题上比 —— 单跳召回、多跳两跳都中、以及预算内能装进多少不同源块

②为什么必须做：命题看起来更干净，不代表检索更好。唯一算数的是
「用问题去找，能不能找到该找到的那一段」。

用法：python tools/proposition-index-probe.py
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
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prop.sql")
MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_grown-docs.json")

CHAT = "qwen3:4b"
EMBED = "bge-m3"
SAMPLE_PER_DOC = 3
BUDGET_TOKENS = 4944
CHARS_PER_TOKEN = 1.5
TOP_K = 12

PROMPT = """把下面这段资料拆成若干条**自足的事实命题**。

硬性要求：
1. 每条命题必须脱离上下文就能读懂 —— 不许出现「它」「该」「此」「上述」「前者」这类指代，
   指代对象要写出来（例如把「它的失效策略」写成「Redis 键的失效策略」）。
2. 只写资料里说过的事实。**不许补充资料之外的任何内容，不许推论**。
   资料里没有的数字、结论、因果关系一律不得出现。
3. 一条命题只讲一件事。资料里同一条事实被反复说，只保留一次。
4. 保持原有术语与数字，不要改写数值、不要换算。

输出 JSON：{"facts":["命题一","命题二"]}"""

SCHEMA = '{"type":"object","properties":{"facts":{"type":"array","items":{"type":"string"}}},"required":["facts"]}'

# 需要覆盖到的主题（与多跳探针同一批，便于对照）
TOPICS = ["Redis 持久化 RDB AOF", "kafka消费组和分区", "Docker 容器网络 namespace",
          "Kubernetes Pod 调度 亲和性", "布隆过滤器 原理 误判率", "一致性哈希 虚拟节点",
          "向量数据库 检索 原理", "模型量化 int8 4bit"]

SINGLE = [("HTTP/2 多路复用 头部压缩", "HTTP/2 的多路复用是怎么回事？"),
          ("Redis 持久化 RDB AOF", "Redis 的 RDB 和 AOF 有什么区别？"),
          ("布隆过滤器 原理 误判率", "布隆过滤器为什么会有误判？"),
          ("向量数据库 检索 原理", "向量数据库是怎么做检索的？")]
MULTI = [("Redis 的 AOF 和 Kafka 的日志复制在持久化思路上有什么不同？",
          "Redis 持久化 RDB AOF", "kafka消费组和分区"),
         ("布隆过滤器和一致性哈希都用了哈希，用途有什么不同？",
          "布隆过滤器 原理 误判率", "一致性哈希 虚拟节点"),
         ("Docker 的 namespace 和 Kubernetes 的 Pod 调度是什么关系？",
          "Docker 容器网络 namespace", "Kubernetes Pod 调度 亲和性"),
         ("模型量化对向量检索的精度有什么影响？",
          "模型量化 int8 4bit", "向量数据库 检索 原理")]

PRONOUN = re.compile(r"(它|该|此|其|上述|前者|后者|本文|本段|上面|这种|这些)")
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
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False,
                           "format": json.loads(SCHEMA),
                           "options": {"temperature": 0.1, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": PROMPT},
                                        {"role": "user", "content": chunk}]})
    txt = r.get("message", {}).get("content", "")
    try:
        return json.loads(txt).get("facts", [])
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                return json.loads(m.group(0)).get("facts", [])
            except Exception:
                pass
    return []


def embed_all(texts):
    out = []
    for i in range(0, len(texts), 16):
        r = post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]}, timeout=300)
        out.extend(r["embeddings"])
    return np.array(out, dtype=np.float32)


def norm(a):
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.maximum(n, 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    man = json.load(open(MANIFEST, encoding="utf-8"))
    by_topic = {}
    for d in man["docs"]:
        if d.get("topic"):
            by_topic.setdefault(d["topic"], []).append(d["docId"])
    need = [t for t in TOPICS if t in by_topic]
    doc_ids = [d for t in need for d in by_topic[t]]
    print(f"取样文档 {len(doc_ids)} 篇（{len(need)} 个主题），每篇 {SAMPLE_PER_DOC} 段\n")

    ids = ",".join("'" + d + "'" for d in doc_ids)
    # 每篇取中间几段：开头常是导航/版权，结尾常是评论
    rows = psql(f"SELECT id, doc_id, content FROM ("
                f"  SELECT id, doc_id, content, row_number() OVER (PARTITION BY doc_id ORDER BY seq) rn "
                f"  FROM chunks WHERE doc_id IN ({ids})) t WHERE rn BETWEEN 2 AND {1 + SAMPLE_PER_DOC};")
    chunks = []
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            chunks.append({"id": p[0], "doc": p[1], "text": p[2]})
    print(f"实际取样 {len(chunks)} 段")

    # ---- ① 抽取 ----
    facts, n_pron, n_hall, flen = [], 0, 0, []
    per_chunk = []
    for i, ch in enumerate(chunks, 1):
        fs = extract(ch["text"])
        per_chunk.append(len(fs))
        src_nums = set(NUM.findall(ch["text"]))
        for f in fs:
            facts.append({"text": f, "doc": ch["doc"], "chunk": ch["id"]})
            flen.append(len(f))
            if PRONOUN.search(f):
                n_pron += 1
            bad = [x for x in NUM.findall(f) if x not in src_nums]
            if bad:
                n_hall += 1
        print(f"  [{i:>2}/{len(chunks)}] {len(fs):>2} 条  {ch['text'][:34]}…", flush=True)

    nf = len(facts)
    print(f"\n—— ① 抽取质量 ——")
    print(f"命题数 {nf}（源块 {len(chunks)}，平均 {nf/len(chunks):.1f} 条/块）")
    print(f"长度：命题均值 {np.mean(flen):.0f} 字，源块均值 "
          f"{np.mean([len(c['text']) for c in chunks]):.0f} 字"
          f"　（压缩到 {100*np.mean(flen)/(np.mean([len(c['text']) for c in chunks])/max(nf/len(chunks),1e-9)):.0f}%"
          f" 的单条比例）")
    print(f"指代残留：{n_pron}/{nf} = {100*n_pron/nf:.0f}%")
    print(f"数字越界（命题里出现原文没有的数）：{n_hall}/{nf} = {100*n_hall/nf:.0f}%")
    empty = sum(1 for x in per_chunk if x == 0)
    print(f"抽不出任何命题的块：{empty}/{len(chunks)}")

    # ---- ② 功能 A/B ----
    print(f"\n—— ② 同一批源料，块索引 vs 命题索引 ——")
    cvec = norm(embed_all([c["text"] for c in chunks]))
    fvec = norm(embed_all([f["text"] for f in facts]))

    def top_docs(q, k):
        v = norm(embed_all([q]))[0]
        ds = cvec @ v
        fs = fvec @ v
        c_top = [chunks[i]["doc"] for i in np.argsort(-ds)[:k]]
        f_top = [facts[i]["doc"] for i in np.argsort(-fs)[:k]]
        return c_top, f_top, ds, fs

    print(f"{'问题':<32}{'块索引':>10}{'命题索引':>10}")
    sc = sf = 0
    for topic, q in SINGLE:
        tgt = set(by_topic.get(topic, []))
        ct, ft, _, _ = top_docs(q, TOP_K)
        a, b = bool(tgt & set(ct)), bool(tgt & set(ft))
        sc += a
        sf += b
        print(f"{q[:30]:<32}{('✅' if a else '❌'):>10}{('✅' if b else '❌'):>10}")
    mc = mf = 0
    for q, t1, t2 in MULTI:
        d1, d2 = set(by_topic.get(t1, [])), set(by_topic.get(t2, []))
        ct, ft, _, _ = top_docs(q, TOP_K)
        a = bool(d1 & set(ct)) and bool(d2 & set(ct))
        b = bool(d1 & set(ft)) and bool(d2 & set(ft))
        mc += a
        mf += b
        print(f"{q[:30]:<32}{('✅' if a else '❌'):>10}{('✅' if b else '❌'):>10}")
    print(f"\n单跳命中　块 {sc}/{len(SINGLE)}　命题 {sf}/{len(SINGLE)}")
    print(f"多跳两跳都中　块 {mc}/{len(MULTI)}　命题 {mf}/{len(MULTI)}")

    # ---- 预算效率 ----
    ctok = [len(c["text"]) / CHARS_PER_TOKEN for c in chunks]
    ftok = [len(f["text"]) / CHARS_PER_TOKEN for f in facts]
    print(f"\n—— ③ 预算 {BUDGET_TOKENS} token 内能装多少「不同源块」——")
    print(f"  源块：{np.mean(ctok):.0f} token/条 → 12 条上限内约 {min(12, int(BUDGET_TOKENS/np.mean(ctok)))} 条，"
          f"覆盖最多 {min(12, int(BUDGET_TOKENS/np.mean(ctok)))} 个不同源块")
    print(f"  命题：{np.mean(ftok):.0f} token/条 → 同样预算约 {int(BUDGET_TOKENS/np.mean(ftok))} 条，"
          f"但它们来自更少的源块（{nf/len(chunks):.1f} 条/块）→ "
          f"约覆盖 {min(len(chunks), int(BUDGET_TOKENS/np.mean(ftok))/(nf/len(chunks))):.0f} 个不同源块")


if __name__ == "__main__":
    main()
