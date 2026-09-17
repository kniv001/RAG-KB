# -*- coding: utf-8 -*-
"""
多跳召回探针 —— 「零碎信息+树+节点」这类结构真正要解决的问题在哪。

上一支探针（单一主题问题）拿到 100% top-1，说明分块层对那种问题不是瓶颈。
但那一类问题的答案就在**一篇文档里**，而命题级索引 / RAPTOR / GraphRAG 这类
结构被提出来的动机是多跳与全局问题：答案需要跨两篇以上文档拼。

所以这里测的是：**一次单查询检索，能不能同时捞到两跳的目标**。

局限（同样要说明）：
  · 只复刻向量通道，且只发一次查询。真实链路会由规划器把这类问题**拆成多条子查询**
    再各查一次 —— 那是本系统已有的动态分解，会比这里的数好。所以这是下界。
  · 目标文档是「人工指定的应命中文档」，不是唯一正确答案。

用法：python tools/multihop-recall-probe.py
"""
import json
import os
import subprocess
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434/api/embed"
MODEL = "bge-m3"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mh.sql")
MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_grown-docs.json")

MAX_DISTANCE = 0.60
K = 12   # maxContexts

# (问题, 跳1主题, 跳2主题)
CASES = [
    ("Redis 的 AOF 和 Kafka 的日志复制在持久化思路上有什么不同？",
     "Redis 持久化 RDB AOF", "kafka消费组和分区"),
    ("布隆过滤器和一致性哈希都用了哈希，用途有什么不同？",
     "布隆过滤器 原理 误判率", "一致性哈希 虚拟节点"),
    ("Docker 的 namespace 和 Kubernetes 的 Pod 调度是什么关系？",
     "Docker 容器网络 namespace", "Kubernetes Pod 调度 亲和性"),
    ("MySQL 的 MVCC 和 Redis 的持久化在保证一致性上有什么不同？",
     "MySQL 事务隔离级别 MVCC", "Redis 持久化 RDB AOF"),
    ("向量数据库的 HNSW 索引和 PostgreSQL 的 B-tree 索引有什么区别？",
     "向量数据库 检索 原理", "B-tree"),
    ("模型量化对向量检索的精度有什么影响？",
     "模型量化 int8 4bit", "向量数据库 检索 原理"),
]


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


def embed(text):
    body = json.dumps({"model": MODEL, "input": [text]}).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embeddings"][0]


def literal(v):
    return "'[" + ",".join(f"{x:.7f}" for x in v) + "]'"


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

    # B-tree 那批是旧文档，按名字找
    bt = psql("SELECT id FROM documents WHERE name LIKE '%B-tree%' OR name LIKE '%btree%';")
    by_topic["B-tree"] = [x.strip() for x in bt.splitlines() if x.strip()]

    print(f"测的是「一次单查询能不能同时捞到两跳」；真实链路的规划器会拆成多条子查询，所以这是下界")
    print(f"{'问题':<34}{'跳1名次':>8}{'跳2名次':>8}{'两跳都有':>9}")
    both = 0
    for q, t1, t2 in CASES:
        d1, d2 = by_topic.get(t1, []), by_topic.get(t2, [])
        v = literal(embed(q))
        out = psql(f"SELECT doc_id FROM chunks WHERE embedding <=> {v} <= {MAX_DISTANCE} "
                   f"ORDER BY embedding <=> {v} LIMIT {K};")
        docs = [l.strip() for l in out.splitlines() if l.strip()]
        r1 = next((i + 1 for i, d in enumerate(docs) if d in d1), None)
        r2 = next((i + 1 for i, d in enumerate(docs) if d in d2), None)
        ok = r1 is not None and r2 is not None
        both += 1 if ok else 0
        qq = q if len(q) <= 32 else q[:31] + "…"
        print(f"{qq:<34}{str(r1 or '—'):>8}{str(r2 or '—'):>8}{('✅' if ok else '❌'):>9}")

    n = len(CASES)
    print(f"\n两跳都进 top-{K}：{both}/{n} = {100*both/n:.0f}%"
          f"　（单跳能进的比例另计：见上表两个名次列）")
    print("说明：名次为空 = 该跳的文档在 top-12 里一次都没出现")


if __name__ == "__main__":
    main()
