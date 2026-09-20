# -*- coding: utf-8 -*-
"""
难题集：现有基准饱和了（单跳 15/15 排第一），得先把「失败」测出来。

现有基准的问法**自带文档专有词**（「G1 垃圾回收器是怎么工作的？」里 G1、垃圾回收器
都是原文词），所以 15/15 是必然的 —— 它测不出任何检索改进。
这一版**用同一批靶子**（15 个主题各 2 篇），只把问法换成人真的会问的样子：

  口语化   「我的 Redis 挂了会不会丢数据？」      ← 全无专有词
  同义     「垃圾收集是怎么回收内存的？」          ← 词面不匹配（原文写 GC）
  场景式   「怎么能让某个服务总跑在同一台机器上？」  ← 描述需求而非术语
  比较式   「Redis 重启后数据还在吗？靠什么保证？」

判据：目标文档的块**进没进 top-30、排第几**。与自带词版本配对比较。

用法：python tools/hard-query-probe.py

【已被 tools/ruler.py 取代】`python tools/ruler.py run hard-query-15 --source raw --k 8,24 --cap same`。
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
HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(HERE, "_hard.sql")
MANIFEST = os.path.join(HERE, "..", "data", "_grown-docs.json")
POOL = 30

# (主题, 自带词问法, 难题问法, 类别)
CASES = [
    ("HTTP/2 多路复用 头部压缩", "HTTP/2 的多路复用是怎么回事？",
     "网页加载慢，从协议层还能怎么优化？", "口语化"),
    ("Redis 持久化 RDB AOF", "Redis 的 RDB 和 AOF 有什么区别？",
     "我的 Redis 挂了，重启之后数据还在吗？", "场景式"),
    ("Kubernetes Pod 调度 亲和性", "Kubernetes 是怎么调度 Pod 的？",
     "怎么能让某个服务总是跑在同一台机器上？", "场景式"),
    ("Docker 容器网络 namespace", "Docker 的 namespace 和 cgroup 分别管什么？",
     "两个容器为什么互相看不见对方？", "口语化"),
    ("JVM 垃圾回收 G1", "G1 垃圾回收器是怎么工作的？",
     "垃圾收集是怎么把内存收回去的？", "同义"),
    ("Python GIL 全局解释器锁", "Python 的 GIL 是什么？",
     "为什么 Python 开多线程跑不快？", "口语化"),
    ("Rust 所有权 借用检查", "Rust 的所有权规则是什么？",
     "Rust 为什么不需要垃圾回收？", "口语化"),
    ("布隆过滤器 原理 误判率", "布隆过滤器为什么会有误判？",
     "怎么快速判断一个网址我爬过没有？", "场景式"),
    ("一致性哈希 虚拟节点", "一致性哈希的虚拟节点解决什么问题？",
     "加机器的时候为什么缓存会大面积失效？", "口语化"),
    ("模型量化 int8 4bit", "模型量化里 int8 和 4bit 有什么区别？",
     "显存不够，怎么把模型压小一点？", "场景式"),
    ("DNS 递归解析 过程", "DNS 递归解析的流程是怎样的？",
     "在浏览器里敲一个网址到页面出来，中间发生了哪些事？", "口语化"),
    ("MySQL 事务隔离级别 MVCC", "MySQL 的 MVCC 是怎么实现事务隔离的？",
     "两个人同时改同一行数据，会发生什么？", "口语化"),
    ("向量数据库 检索 原理", "向量数据库是怎么做检索的？",
     "怎么在上百万条里飞快地找出最像的那几条？", "口语化"),
    ("提示词工程 上下文 窗口", "提示词工程里怎么处理上下文窗口？",
     "模型老是忘掉我前面提的要求，怎么办？", "口语化"),
    ("微服务 熔断 降级", "微服务的熔断和降级是什么？",
     "一个服务挂了，怎么才能不把整个系统拖垮？", "场景式"),
]


def psql(sql):
    open(SQL_FILE, "w", encoding="utf-8").write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE],
                       capture_output=True, env=env)
    if p.returncode != 0:
        print("psql 失败:", p.stderr.decode("utf-8", "backslashreplace")[:300])
        sys.exit(1)
    return p.stdout.decode("utf-8", "replace")


def embed(text):
    body = json.dumps({"model": MODEL, "input": [text]}).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embeddings"][0]


def literal(v):
    return "'[" + ",".join(f"{x:.7f}" for x in v) + "]'"


def rank_of(q, doc_ids):
    v = literal(embed(q))
    out = psql(f"SELECT doc_id FROM chunks ORDER BY embedding <=> {v} LIMIT {POOL};")
    for i, line in enumerate(out.splitlines()):
        if line.strip() in doc_ids:
            return i + 1
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    man = json.load(open(MANIFEST, encoding="utf-8"))
    by_topic = {}
    for d in man["docs"]:
        if d.get("topic"):
            by_topic.setdefault(d["topic"], set()).add(d["docId"])

    print(f"{'类别':<7}{'自带词问法':<30}{'rank':>5}   {'难题问法':<34}{'rank':>5}")
    tot_easy = tot_hard = 0
    hard_ranks = []
    for topic, easy_q, hard_q, kind in CASES:
        ids = by_topic.get(topic, set())
        if not ids:
            continue
        re_ = rank_of(easy_q, ids)
        rh = rank_of(hard_q, ids)
        tot_easy += 1 if re_ else 0
        tot_hard += 1 if rh else 0
        hard_ranks.append(rh)
        mark = lambda r: f"{r}" if r else "未进池"
        print(f"{kind:<7}{easy_q[:28]:<30}{mark(re_):>5}   {hard_q[:32]:<34}{mark(rh):>5}")

    n = len(hard_ranks)
    top4h = sum(1 for r in hard_ranks if r and r <= 4)
    top12h = sum(1 for r in hard_ranks if r and r <= 12)
    print(f"\n自带词问法：进池 {tot_easy}/{n}　难题问法：进池 {tot_hard}/{n}")
    print(f"难题 top-4 命中 {top4h}/{n}　top-12 命中 {top12h}/{n}　top-30 命中 {tot_hard}/{n}")
    if tot_hard < n:
        print("\n掉出池子的（真正的失败）：")
        for (topic, _, hq, kind), r in zip(CASES, hard_ranks):
            if not r:
                print(f"  [{kind}] {hq}")


if __name__ == "__main__":
    main()
