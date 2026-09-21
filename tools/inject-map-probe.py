# -*- coding: utf-8 -*-
"""
**把"扫描"从生成侧挪到输入侧**：注入一份代码生成的资料地图。

用户的关键观察（2026-09-21）：**目标是省"生成思考"的时间，不是省"思考用的 token"**。
而 prefill 3850 tok/s vs decode 77 tok/s —— **差 50 倍**。所以：

    注入的思考几乎不要钱，生成的思考很贵。

⇒ "少想"的正确形状不是**压短**，是**把内容从生成侧挪到输入侧**。之前那些限制
（"最多 4 条"、"不要复述资料"）全在逼它**少生成**，方向就错了。

而今天量过：思考里 **扫描 15% + 引原文 8% + 复述规则 4% = 27% 是纯扫描/抄写** ——
那部分**正是代码能替的**（编号、文档、块号、语境行全在库里）。

本探针只验一件事：**注入一大段资料地图，decode token 会不会掉。**
（质量由 `tools/eval.py` 那两把尺子管，不在这里看。）

用法：python tools/inject-map-probe.py [次数，默认 3]
"""
import io
import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"

SYS = "你是个人知识库助手。严格依据【参考资料】回答问题。引用处用 [编号] 标注来源。"

# 一份**真实形态**的资料（15 段，取自本机语料的两篇 Redis 文章）
DOCS = [
    ("Redis持久化原理 — RDB与AOF详细解释", 13,
     "而一旦新 AOF 文件创建完毕，Redis 就会从旧 AOF 文件切换到新 AOF 文件，并开始对新 AOF 文件进行追加操作。"),
    ("Redis持久化原理 — RDB与AOF详细解释", 8,
     "TA磁盘只有几百TPS | everysec 每秒进行与fsync，最多丢失1秒数据 | no 不主动fsync，由操作系统决定何时同步。"),
    ("Redis持久化原理 — RDB与AOF详细解释", 6,
     "AOF也需要fork，但是你可以调节重写日志文件的频率来提高性能。"),
    ("Redis持久化之RDB&AOF", 14,
     "AOF 文件会变得庞大，但 redis 有优化策略，比如你对一个 key1 键的操作，set key1 001, set key1 002，"
     "set key1 003，那优化的结果就是将前两条去掉。"),
    ("Redis持久化之RDB&AOF", 2,
     "Fork 发生时，父子进程内存共享，所以为了不影响子进程做数据快照，在这期间修改的数据将会被复制一份。"),
    ("Redis持久化之RDB&AOF", 3,
     "并不会立即将命令写入到硬盘文件中，而是写入到硬盘缓存，在接下来的策略中，配置多久来从硬盘缓存写入到硬盘文件。"),
]
DOC = "【参考资料】\n" + "\n".join(
    f"[{i+1}] 来源：{d}（第 {s} 块）\n{c}" for i, (d, s, c) in enumerate(DOCS))
Q = "AOF 的刷盘策略有哪几种？最坏情况下会丢多少数据？"

# —— 代码生成的"资料地图"：编号 / 文档 / 块号 / 语境行 / 去重提示，全在库里 ——
MAP = """【资料地图（系统预生成，**直接用，不必自己核对**）】
本次召回 6 段，按文档分组：

■ Redis持久化原理 — RDB与AOF详细解释（3 段）
  [1] 第 13 块 · 本段可回答：AOF 重写期间的新写命令会丢吗
  [2] 第 8 块  · 本段可回答：appendfsync 的三个取值各是什么
  [3] 第 6 块  · 本段可回答：AOF 重写为什么要 fork

■ Redis持久化之RDB&AOF（3 段）
  [4] 第 14 块 · 本段可回答：AOF 文件为什么会越来越大
  [5] 第 2 块  · 本段可回答：fork 时父子进程的内存怎么处理
  [6] 第 3 块  · 本段可回答：写命令是立刻落盘还是先进缓存

**提示**：[1] 与 [4] 讲的是同一件事的两个侧面（重写与膨胀）；
[5] 与 [3] 都在讲 fork，不必分别核对。[6] 直接对应"最坏丢多少"。
"""


def chat(user, timeout=400):
    body = {"model": MODEL, "stream": False, "think": True,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": user}],
            "options": {"temperature": 0.2, "num_ctx": 8192}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    wall = time.time() - t0
    m = d.get("message", {})
    return (m.get("thinking", "") or ""), (m.get("content", "") or ""), \
        d.get("eval_count", 0), d.get("prompt_eval_count", 0), wall


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for label, extra in [("A 现状（只给资料）", ""),
                         ("B 现状 + 资料地图", MAP)]:
        print(f"===== {label}")
        rows = []
        for i in range(n):
            th, ct, gen, pj, wall = chat(DOC + extra + f"\n【问题】\n{Q}")
            rows.append((gen, pj, len(th), len(ct), wall))
            print(f"  {i+1}. 生成 {gen:>5} tok（思考 {len(th):>5} 字 / 正文 {len(ct):>4} 字）"
                  f"　装入 {pj:>5} tok　墙钟 {wall:.1f}s")
        med = lambda k: sorted(r[k] for r in rows)[len(rows)//2]
        print(f"  ⇒ 生成中位 {med(0)}　装入中位 {med(1)}　思考 {med(2)} 字　墙钟中位 {med(4):.1f}s\n")


if __name__ == "__main__":
    main()
