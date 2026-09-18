# -*- coding: utf-8 -*-
"""
多装几块到底有没有用：13 块 vs 24 块，判据=答案复现「目标文档独有词」。

为什么问这个：③②（装饰瘦身 + 邻接合并）实测能腾出约 12% 的提示词，
但**预算只用 17~29%** —— 腾出来的地方本来是空的。所以它们只有在"腾出来是为了多装"
时才值得做。而这个前提从没验过：上一轮 A/B（top-k 4→8）结果是「引用来源数 +47%、
严格判据只 +0.5 点，属噪声级」。

现在有两个新条件让它值得重测：① 语境行已落地（索引变好）；② 有了难题集这种敏感判据。

判据（机械，不靠模型自评）：先把目标文档的「独有词」选出来 ——
在目标文档里出现、且全库其他文档里几乎不出现的 4 字串，取频次最高的 40 个；
再看生成的答案里出现了多少个。**这是"有没有真的用上那份资料"的硬指标。**

用法：python tools/pool-size-ab.py [题数，默认 6]
"""
import io
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
MANIFEST = os.path.join(HERE, "..", "data", "_grown-docs.json")
SYS_FILE = os.path.join(HERE, "..", "data", "_answer_system.txt")
CHAT, EMBED = "qwen3:4b", "bge-m3"
BS = chr(92)

# (主题, 问法)　混一半"人话问法"
CASES = [
    ("JVM 垃圾回收 G1", "G1 垃圾回收器是怎么工作的？"),
    ("Redis 持久化 RDB AOF", "我的 Redis 挂了，重启之后数据还在吗？"),
    ("Kubernetes Pod 调度 亲和性", "怎么能让某个服务总是跑在同一台机器上？"),
    ("一致性哈希 虚拟节点", "一致性哈希的虚拟节点解决什么问题？"),
    ("微服务 熔断 降级", "一个服务挂了，怎么才能不把整个系统拖垮？"),
    ("DNS 递归解析 过程", "在浏览器里敲一个网址到页面出来，中间发生了哪些事？"),
]


def psql(sql):
    f = os.path.join(HERE, "_ab.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    out = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    rows = []
    for line in out.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        for a, b in ((BS + BS, BS), (BS + "n", "\n"), (BS + "t", "\t")):
            line = line.replace(a, b)
        rows.append(line)
    return rows


def embed(text):
    body = json.dumps({"model": EMBED, "input": [text]}).encode()
    req = urllib.request.Request(OLLAMA + "/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["embeddings"][0]


def distinctive_terms(doc_ids, top=40):
    """目标文档独有词：在目标里出现、全库其他文档里几乎不出现的 4 字串。"""
    rows = psql("SELECT doc_id, content FROM chunks")
    target, others = [], []
    for line in rows:
        p = line.split("\t", 1)
        if len(p) != 2:
            continue
        (target if p[0] in doc_ids else others).append(p[1])
    tgt = "".join(target)
    oth = "".join(others)
    freq = {}
    for i in range(len(tgt) - 4):
        g = tgt[i:i + 4]
        if re.search(r"[^\u4e00-\u9fffA-Za-z0-9]", g):
            continue
        freq[g] = freq.get(g, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: -kv[1])
    out = []
    for g, c in ranked:
        if c < 2:
            continue
        if oth.count(g) > 1:
            continue
        out.append(g)
        if len(out) >= top:
            break
    return out


def answer(question, chunks, sysmsg):
    parts = []
    for i, (name, seq, body) in enumerate(chunks, 1):
        parts.append(f"[{i}] 来源：{name}（第 {seq} 块）\n{body}")
    user = "【参考资料】\n" + "\n\n".join(parts) + "\n\n【问题】\n" + question
    body = {"model": CHAT, "stream": False, "think": True,
            "options": {"temperature": 0.1, "num_ctx": 16384},
            "messages": [{"role": "system", "content": sysmsg},
                         {"role": "user", "content": user}]}
    for _ in range(2):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=600))
            return d.get("message", {}).get("content", "").strip()
        except Exception:
            time.sleep(3)
    return "<失败>"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else len(CASES)
    sysmsg = io.open(SYS_FILE, encoding="utf-8").read().strip()
    man = json.load(io.open(MANIFEST, encoding="utf-8"))
    by = {}
    for d in man["docs"]:
        if d.get("topic"):
            by.setdefault(d["topic"], set()).add(d["docId"])

    print(f"答案用线上那版 ANSWER_SYSTEM（{len(sysmsg)} 字）\n")
    print(f"{'问题':<30}{'13 块覆盖':>10}{'24 块覆盖':>10}")
    tot13 = tot24 = 0
    for topic, q in CASES[:n]:
        ids = by.get(topic, set())
        terms = distinctive_terms(ids)
        v = "[" + ",".join(f"{x:.7f}" for x in embed(q)) + "]"
        rows = psql(f"SELECT d.name, c.seq, c.content FROM chunks c JOIN documents d ON d.id=c.doc_id "
                    f"WHERE c.embedding IS NOT NULL ORDER BY c.embedding <=> '{v}' LIMIT 24")
        chunks = []
        for line in rows:
            p = line.split("\t", 2)
            if len(p) == 3:
                chunks.append((p[0], p[1], p[2]))
        c13 = sum(1 for t in terms if t in answer(q, chunks[:13], sysmsg))
        c24 = sum(1 for t in terms if t in answer(q, chunks[:24], sysmsg))
        tot13 += c13
        tot24 += c24
        print(f"{q[:28]:<30}{c13:>7}/{len(terms)}{c24:>7}/{len(terms)}", flush=True)
    print(f"\n合计独有词覆盖：13 块 {tot13}　24 块 {tot24}　差 {tot24 - tot13:+d}")


if __name__ == "__main__":
    main()
