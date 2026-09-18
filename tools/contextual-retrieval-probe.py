# -*- coding: utf-8 -*-
"""
Contextual Retrieval：给「入库时喂给向量的文本」加语境，看难题排名动不动。

背景（见 2026-09-18-难题的失败是排序不是名额）：难题集 13/15，两条失败的靶子排在
**rank 43 / 50** —— 问句的词与文档的词在向量空间里够不着，加宽召回无用。

三档，逐档加料，看哪一档够得着（都用同一批难题、同一批靶子）：
  A 现状        只嵌 content
  B 标题前置    嵌 <文档名>\n<content> —— **零 LLM 成本**，标题里常有机制词
  C LLM 语境行  嵌 <文档名>\n<生成的语境行>\n<content> —— Anthropic 那套的廉价版
                （只给「标题 + 前后各一块」，不喂全文，否则 661 次调用每次五六千 token）

判据：15 条难题里**靶子进 top-30 的条数**与**最佳排名**。A 档是已知基线（13/15）。

用法：python tools/contextual-retrieval-probe.py [A|B|C]
"""
import io
import json
import os
import subprocess
import sys
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "data", "_grown-docs.json")
CACHE = os.path.join(HERE, "..", "data", "_ctx-cache.json")
EMBED, CHAT = "bge-m3", "qwen3:4b"
POOL = 30
BS = chr(92)

HARD = [
    ("HTTP/2 多路复用 头部压缩", "网页加载慢，从协议层还能怎么优化？"),
    ("Redis 持久化 RDB AOF", "我的 Redis 挂了，重启之后数据还在吗？"),
    ("Kubernetes Pod 调度 亲和性", "怎么能让某个服务总是跑在同一台机器上？"),
    ("Docker 容器网络 namespace", "两个容器为什么互相看不见对方？"),
    ("JVM 垃圾回收 G1", "垃圾收集是怎么把内存收回去的？"),
    ("Python GIL 全局解释器锁", "为什么 Python 开多线程跑不快？"),
    ("Rust 所有权 借用检查", "Rust 为什么不需要垃圾回收？"),
    ("布隆过滤器 原理 误判率", "怎么快速判断一个网址我爬过没有？"),
    ("一致性哈希 虚拟节点", "加机器的时候为什么缓存会大面积失效？"),
    ("模型量化 int8 4bit", "显存不够，怎么把模型压小一点？"),
    ("DNS 递归解析 过程", "在浏览器里敲一个网址到页面出来，中间发生了哪些事？"),
    ("MySQL 事务隔离级别 MVCC", "两个人同时改同一行数据，会发生什么？"),
    ("向量数据库 检索 原理", "怎么在上百万条里飞快地找出最像的那几条？"),
    ("提示词工程 上下文 窗口", "模型老是忘掉我前面提的要求，怎么办？"),
    ("微服务 熔断 降级", "一个服务挂了，怎么才能不把整个系统拖垮？"),
]

CTX_PROMPT = """你在给检索索引加语境。下面是一篇文档的标题、这个片段的位置，以及片段本身。
写**一句**说明：这个片段讲的是什么问题、在全文里承担什么角色。

要求：
- 用读者会用的词，而不是照抄片段里的术语 —— 检索时问的人往往不知道术语
- 不超过 40 字
- 只输出 JSON：{"ctx":"..."}"""


def psql(sql):
    f = os.path.join(HERE, "_ctx.sql")
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
        for a, b in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(a, b)
        rows.append(line)
    return rows


def embed(texts):
    out = []
    for i in range(0, len(texts), 8):
        body = json.dumps({"model": EMBED, "input": texts[i:i + 8]}).encode()
        req = urllib.request.Request(OLLAMA + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        out.extend(json.load(urllib.request.urlopen(req, timeout=300))["embeddings"])
    a = np.array(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    mode = (sys.argv[1] if len(sys.argv) > 1 else "B").upper()

    rows = psql("SELECT c.id, c.doc_id, d.name, c.seq, c.content FROM chunks c "
                "JOIN documents d ON d.id = c.doc_id ORDER BY c.doc_id, c.seq")
    items = []
    for line in rows:
        p = line.split("\t", 4)
        if len(p) == 5:
            items.append(dict(id=p[0], docid=p[1], doc=p[2], seq=int(p[3]), text=p[4]))
    print(f"块 {len(items)}　档位 {mode}")

    ctx = {}
    if mode == "C":
        ctx = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
        todo = [x for x in items if x["id"] not in ctx]
        print(f"语境行已有 {len(ctx)}，待生成 {len(todo)}（每块一次 4b 调用）", flush=True)
        body_tpl = [
            ("title", "标题：《%s》"),
            ("pos", "位置：第 %d 块"),
            ("text", "片段：\n%s"),
        ]
        for k, x in enumerate(todo, 1):
            user = (f"标题：《{x['doc']}》\n位置：第 {x['seq']} 块\n片段：\n{x['text']}")
            body = {"model": CHAT, "stream": False, "think": False,
                    "format": {"type": "object", "properties": {"ctx": {"type": "string"}},
                               "required": ["ctx"]},
                    "options": {"temperature": 0.2, "num_ctx": 8192},
                    "messages": [{"role": "system", "content": CTX_PROMPT},
                                 {"role": "user", "content": user}]}
            try:
                req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                raw = json.load(urllib.request.urlopen(req, timeout=180))["message"]["content"]
                ctx[x["id"]] = json.loads(raw).get("ctx", "").strip()
            except Exception as e:
                ctx[x["id"]] = ""
            if k % 50 == 0:
                json.dump(ctx, io.open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
                print(f"  {k}/{len(todo)}", flush=True)
        json.dump(ctx, io.open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

    def text_for(x):
        if mode == "A":
            return x["text"]
        if mode == "B":
            return f"{x['doc']}\n{x['text']}"
        return f"{x['doc']}\n{ctx.get(x['id'], '')}\n{x['text']}"

    V = embed([text_for(x) for x in items])
    man = json.load(io.open(MANIFEST, encoding="utf-8"))
    by = {}
    for d in man["docs"]:
        if d.get("topic"):
            by.setdefault(d["topic"], set()).add(d["docId"])
    qv = embed([q for _, q in HARD])
    hit = 0
    ranks = []
    print(f"\n{'难题':<34}{'最佳排名':>9}")
    for (topic, q), v in zip(HARD, qv):
        ids = by.get(topic, set())
        sims = V @ v
        order = np.argsort(-sims)[:POOL]
        r = next((i + 1 for i, j in enumerate(order) if items[j]["docid"] in ids), None)
        ranks.append(r)
        hit += 1 if r else 0
        print(f"{q[:32]:<34}{(r if r else '未进池'):>9}")
    top4 = sum(1 for r in ranks if r and r <= 4)
    print(f"\n进 top-{POOL}：{hit}/{len(HARD)}　进 top-4：{top4}/{len(HARD)}")


if __name__ == "__main__":
    main()
