# -*- coding: utf-8 -*-
"""
分块层的召回基线 —— 评「要不要重建知识库表示」之前必须先有的数。

为什么现在能测：上一轮扩语料是**按主题组织**的（每主题 2 篇，主题↔docId 已知），
于是「这个问题的正确答案是哪几段」第一次有了标准答案。在此之前，
所有关于召回好坏的判断都只能靠「回答看起来对不对」，而那些是模型的输出，
不可判定（README 里写明这类断言失败率约三成）。

测什么：
  · 目标文档的块在 top-4 / top-12 / top-30 里出现过没有（召回率）
  · 第一次出现在第几名（排序质量）
  · 最像的那段离目标有多远（是不是就差一点）

局限（必须说清楚）：
  · 只复刻了向量通道。真实检索是 向量+关键词 RRF，关键词通道在这里缺席 → 偏低
  · 用的是原始问句，真实链路用的是规划器改写后的查询 → 也偏低
  所以这里的数是**下界**。

用法：python tools/recall-baseline-probe.py
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
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_recall.sql")
MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_grown-docs.json")

MAX_DISTANCE = 0.60
POOL = 30

# 主题 → 自然提问（人工写的，模拟真实用户问法）
CASES = [
    ("HTTP/2 多路复用 头部压缩", "HTTP/2 的多路复用是怎么回事？"),
    ("Redis 持久化 RDB AOF", "Redis 的 RDB 和 AOF 有什么区别？"),
    ("Kubernetes Pod 调度 亲和性", "Kubernetes 是怎么调度 Pod 的？"),
    ("Docker 容器网络 namespace", "Docker 的 namespace 和 cgroup 分别管什么？"),
    ("JVM 垃圾回收 G1", "G1 垃圾回收器是怎么工作的？"),
    ("Python GIL 全局解释器锁", "Python 的 GIL 是什么？"),
    ("Rust 所有权 借用检查", "Rust 的所有权规则是什么？"),
    ("布隆过滤器 原理 误判率", "布隆过滤器为什么会有误判？"),
    ("一致性哈希 虚拟节点", "一致性哈希的虚拟节点解决什么问题？"),
    ("模型量化 int8 4bit", "模型量化里 int8 和 4bit 有什么区别？"),
    ("DNS 递归解析 过程", "DNS 递归解析的流程是怎样的？"),
    ("MySQL 事务隔离级别 MVCC", "MySQL 的 MVCC 是怎么实现事务隔离的？"),
    ("向量数据库 检索 原理", "向量数据库是怎么做检索的？"),
    ("提示词工程 上下文 窗口", "提示词工程里怎么处理上下文窗口？"),
    ("微服务 熔断 降级", "微服务的熔断和降级是什么？"),
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
    print(f"语料：{psql('SELECT count(*), count(distinct md5(content)) FROM chunks;').strip()}（总段/不同内容）")
    print(f"带标注的主题：{len(by_topic)} 个\n")
    print(f"{'问题':<30}{'目标段':>6}{'top4':>6}{'top12':>7}{'top30':>7}{'最佳名次':>9}{'最近距离':>9}")
    hits = {4: 0, 12: 0, 30: 0}
    ranks, ds, n = [], [], 0
    for topic, q in CASES:
        doc_ids = by_topic.get(topic, [])
        if not doc_ids:
            continue
        n += 1
        ids = ",".join("'" + d + "'" for d in doc_ids)
        v = literal(embed(q))
        out = psql(
            f"SELECT id, doc_id, round((embedding <=> {v})::numeric, 4) AS d "
            f"FROM chunks WHERE embedding <=> {v} <= {MAX_DISTANCE} "
            f"ORDER BY embedding <=> {v} LIMIT {POOL};")
        rows = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) >= 3:
                try:
                    rows.append((p[0], p[1], float(p[2])))
                except ValueError:
                    pass
        rank = next((i + 1 for i, r in enumerate(rows) if r[1] in doc_ids), None)
        best = rows[0][2] if rows else None
        for k in hits:
            if rank and rank <= k:
                hits[k] += 1
        if rank:
            ranks.append(rank)
            ds.append(best)
        tgt = psql(f"SELECT count(*) FROM chunks WHERE doc_id IN ({ids});").strip()
        qq = q if len(q) <= 28 else q[:27] + "…"
        print(f"{qq:<30}{tgt:>6}{(('✅' if rank and rank<=4 else '—')):>6}"
              f"{(('✅' if rank and rank<=12 else '—')):>7}"
              f"{(('✅' if rank else '❌')):>7}{str(rank or '—'):>9}"
              f"{(f'{best:.3f}' if best else '—'):>9}")

    print(f"\n召回率（下界）   top-4 {hits[4]}/{n} = {100*hits[4]/n:.0f}%"
          f"   top-12 {hits[12]}/{n} = {100*hits[12]/n:.0f}%"
          f"   top-30 {hits[30]}/{n} = {100*hits[30]/n:.0f}%")
    if ranks:
        print(f"命中时的名次：最好 {min(ranks)}，最差 {max(ranks)}，"
              f"平均 {sum(ranks)/len(ranks):.1f}；向量距离均值 {sum(ds)/len(ds):.3f}")


if __name__ == "__main__":
    main()
