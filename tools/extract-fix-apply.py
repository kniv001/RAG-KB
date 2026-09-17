# -*- coding: utf-8 -*-
"""
治抽取退化 · 应用与复验。

前面的实测把病因定下来了：
  · 参数不是病因 —— 现状那档（think:false + format）就是四档里最好的
  · 空响应是**模型读了资料之后主动选择不写**（prompt_eval_count 正常 ~500，
    而 eval_count 只有 7，正常要 250~350）
  · 基线空率 4~5/74 ≈ 5~7%，其中约一半可复现、一半是单次抖动；无位置规律
  · 可复现的那几段多半是从文章中间截断的片段（开头结尾都是半句）

所以治两条：
  ① 提示词里**禁止因不完整而弃抽**（这是逃生口的反面 —— 明说「片段也要照抽」）
  ② 空结果重试（抖动那一半靠重试就能捞回来）

复验两件事：最终空率降到多少；以及抽取补全之后，命题索引在同一批问题上
还输不输给块索引（上一轮的 A/B 是被 72% 漏抽污染的，不作数）。

用法：python tools/extract-fix-apply.py
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
DATA = os.path.join(HERE, "..", "data", "_prop-probe.json")
OUT = os.path.join(HERE, "..", "data", "_prop-fixed.json")
SQL_FILE = os.path.join(HERE, "_fix.sql")
CHAT, EMBED, RETRIES, TOP_K = "qwen3:4b", "bge-m3", 3, 12

PROMPT = """把下面这段资料拆成若干条**自足的事实命题**。

硬性要求：
1. 每条命题必须脱离上下文就能读懂 —— 不许出现「它」「该」「此」「上述」「前者」这类指代，
   指代对象要写出来。
2. 只写资料里说过的事实。**不许补充资料之外的任何内容，不许推论**。
   资料里没有的数字、结论、因果关系一律不得出现。
3. 一条命题只讲一件事。同一条事实被反复说，只保留一次。
4. 保持原有术语与数字，不要改写数值。
5. 这段资料多半是从文章中间**截断**出来的，开头结尾是半句话属于正常。
   照抽不误 —— 凡是资料里**明确出现过**的具体陈述（数字、定义、步骤、原因、结论、
   配置项、函数名、参数含义）都要抽出来。不要因为资料不完整、缺少前后文就不输出。"""

SCHEMA = {"type": "object",
          "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
          "required": ["facts"]}

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


def extract(chunk):
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                           "options": {"temperature": 0.1, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": PROMPT},
                                        {"role": "user", "content": chunk}]})
    c = r.get("message", {}).get("content", "")
    try:
        return json.loads(c).get("facts", []), r.get("eval_count")
    except Exception:
        m = re.search(r'"facts"\s*:\s*\[(.*?)\]', c, re.S)
        if m:
            return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)), r.get("eval_count")
    return [], r.get("eval_count")


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

    print(f"—— 抽取（禁止弃抽 + 空结果重试，最多 {RETRIES} 次）——")
    facts, per_attempt = [], [0, 0, 0]
    still_empty = []
    t0 = time.time()
    for i, ch in enumerate(chunks, 1):
        fs = []
        for a in range(RETRIES):
            fs, ec = extract(ch["text"])
            if fs:
                per_attempt[a] += 1
                break
        if not fs:
            still_empty.append(i)
        for f in fs:
            facts.append({"text": f, "doc": ch["doc"], "chunk": ch["id"]})
        print(f"  [{i:>2}/{len(chunks)}] {len(fs):>2} 条{'' if fs else '  ← 仍为空'}", flush=True)

    print(f"\n耗费 {time.time()-t0:.0f}s")
    print(f"三次尝试内抽出：第1次 {per_attempt[0]}，第2次 {per_attempt[1]}，"
          f"第3次 {per_attempt[2]}；重试后仍空 {len(still_empty)}/{len(chunks)}"
          f" = {100*len(still_empty)/len(chunks):.1f}%")
    print(f"仍空的位置：{still_empty}")
    print(f"命题总数 {len(facts)}，平均 {len(facts)/len(chunks):.1f} 条/块")
    json.dump({"facts": facts}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---- A/B ----
    topic_docs = {}
    for t, pats in TOPICS.items():
        ids = set()
        for p in pats:
            ids |= {x.strip() for x in psql(f"SELECT id FROM documents WHERE name LIKE '{p}';").splitlines() if x.strip()}
        topic_docs[t] = ids
        assert ids, t
    cvec = norm(embed_all([c["text"] for c in chunks]))
    fvec = norm(embed_all([f["text"] for f in facts]))
    fd = [f["doc"] for f in facts]
    fc = [f["chunk"] for f in facts]

    def pick(sim, cap):
        order = np.argsort(-sim)
        used, out = {}, []
        for i in order:
            if cap and used.get(fc[i], 0) >= cap:
                continue
            used[fc[i]] = used.get(fc[i], 0) + 1
            out.append(int(i))
            if len(out) >= TOP_K:
                break
        return out

    print(f"\n—— A/B（抽取补全后重测）——")
    print(f"{'问题':<34}{'块索引':>9}{'命题∞':>9}{'命题≤1':>9}{'块覆盖':>8}{'命题≤1覆盖':>11}")
    cases = [(t, q, None) for t, q in SINGLE] + [(None, q, (a, b)) for q, a, b in MULTI]
    hit = {"c": 0, "f0": 0, "f1": 0}
    cov = {"c": [], "f0": [], "f1": []}
    for t, q, pair in cases:
        v = norm(embed_all([q]))[0]
        ci = list(np.argsort(-(cvec @ v))[:TOP_K])
        f0 = list(np.argsort(-(fvec @ v))[:TOP_K])
        f1 = pick(fvec @ v, 1)
        if pair:
            g = {"c": all(any(chunks[i]["doc"] in topic_docs[x] for i in ci) for x in pair),
                 "f0": all(any(fd[i] in topic_docs[x] for i in f0) for x in pair),
                 "f1": all(any(fd[i] in topic_docs[x] for i in f1) for x in pair)}
        else:
            g = {"c": any(chunks[i]["doc"] in topic_docs[t] for i in ci),
                 "f0": any(fd[i] in topic_docs[t] for i in f0),
                 "f1": any(fd[i] in topic_docs[t] for i in f1)}
        for k in hit:
            hit[k] += g[k]
        cov["c"].append(len({chunks[i]["id"] for i in ci}))
        cov["f0"].append(len({fc[i] for i in f0}))
        cov["f1"].append(len({fc[i] for i in f1}))
        print(f"{q[:32]:<34}{('✅' if g['c'] else '❌'):>9}{('✅' if g['f0'] else '❌'):>9}"
              f"{('✅' if g['f1'] else '❌'):>9}{cov['c'][-1]:>8}{cov['f1'][-1]:>11}")
    n = len(cases)
    print(f"\n命中 {n} 题：块 {hit['c']}　命题∞ {hit['f0']}　命题≤1 {hit['f1']}")
    print(f"top-12 平均覆盖源块：块 {np.mean(cov['c']):.1f}　"
          f"命题∞ {np.mean(cov['f0']):.1f}　命题≤1 {np.mean(cov['f1']):.1f}")


if __name__ == "__main__":
    main()
