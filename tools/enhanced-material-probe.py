# -*- coding: utf-8 -*-
"""
**no-think + 分工 的增强：把"组织"这一步由代码在资料里做掉。**

分清两条路径（2026-09-21 澄清，此前测错了地方）：

| 路径 | 多给资料会怎样 |
|---|---|
| `think:true`（思考开着） | **反效果** —— 生成 3141 → **6793 token（2.2×）**，给什么它想什么 |
| **`no-think`（思考关着）** | **没有思考可以放大** ⇒ 资料的完善程度**直接决定答案质量** |

而实测到的问题正在 no-think 这一侧：**分工版退化成资料摘抄**（逐字重合 **86%**，
对照不分工版只有 21%）—— 因为**它没有思考去组织，而资料又是原始的**，它只能抄。

所以增强的方向是：**把"组织"这一步由代码在资料里做掉**。而它是机械的：

  · 每块的**语境行**（「本段可回答什么」）—— 库里本来就有
  · **关键句**：块内与问题词重叠最高的那一句（代码抽取，不调模型）
  · **去重提示**：哪几块讲的是同一件事
  · **分组**：按文档归拢

判据（与 `tools/eval/judges.py` 同一套口径）：
  · **逐字重合**（组织度的代理）—— 越低说明越是在组织而不是抄
  · 数字/术语落地、引用有效性

用法：python tools/enhanced-material-probe.py [次数，默认 3]
"""
import io
import json
import re
import statistics
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"

SYS = ("你是个人知识库助手。严格依据【参考资料】回答问题。"
       "引用处用 [编号] 标注来源。**直接写答案，不要复述资料原文。**")

# 真实形态：两篇 Redis 文章、6 段（含一段与主题无关的，模拟召回噪声）
CHUNKS = [
    ("Redis持久化原理 — RDB与AOF详细解释", 13, "AOF 重写",
     "而一旦新 AOF 文件创建完毕，Redis 就会从旧 AOF 文件切换到新 AOF 文件，并开始对新 AOF 文件进行追加操作。"),
    ("Redis持久化原理 — RDB与AOF详细解释", 8, "appendfsync 三个取值",
     "always 每次收到写命令就立即强制写入磁盘，最慢但保证完全持久化，不推荐；"
     "everysec 每秒进行 fsync，最多丢失 1 秒数据，是性能和持久性的折中，也是默认值；"
     "no 不主动 fsync，完全依赖操作系统，性能最好但持久化没保证。"),
    ("Redis持久化原理 — RDB与AOF详细解释", 6, "AOF 重写为什么要 fork",
     "AOF 也需要 fork，但是你可以调节重写日志文件的频率来提高性能。"),
    ("Redis持久化之RDB&AOF", 14, "AOF 文件为什么会越来越大",
     "AOF 文件会变得庞大，但 redis 有优化策略，比如你对一个 key1 键的操作，set key1 001，"
     "set key1 002，set key1 003，那优化的结果就是将前两条去掉。"),
    ("Redis持久化之RDB&AOF", 2, "fork 时父子进程的内存怎么处理",
     "Fork 发生时，父子进程内存共享，所以为了不影响子进程做数据快照，在这期间修改的数据将会被复制一份。"),
    ("docker容器网络 - 世界的尽头", 4, "none 网络模式（与本问题无关）",
     "这种网络模式下容器只有 lo 回环网络，没有其他网卡，没有办法联网。"),
]
Q = "AOF 的刷盘策略有哪几种？最坏情况下会丢多少数据？"

PLAIN = "【参考资料】\n" + "\n".join(
    f"[{i+1}] 来源：{d}（第 {s} 块）\n{c}" for i, (d, s, _, c) in enumerate(CHUNKS))

# —— 代码增强：关键句抽取 + 语境行 + 去重/无关提示 + 分组 ——
_STOP = set("的了吗呢和与及或在是有为对从把被这那你我他它一个如何什么怎么哪些"
            "为什么么样可以需要应该会能要不".replace(" ", ""))


def key_sentence(text, question, maxlen=90):
    """块内与问题词重叠最高的那一句。纯机械，不调模型。"""
    qs = {c for c in re.sub(r"\s+", "", question) if c not in _STOP}
    sents = [s.strip() for s in re.split(r"[。；\n]", text) if len(s.strip()) >= 8]
    if not sents:
        return text[:maxlen]
    best = max(sents, key=lambda s: len({c for c in re.sub(r"\s+", "", s) if c not in _STOP} & qs))
    return best[:maxlen]


def build_map(chunks, question):
    """把"组织"这一步做掉：分组 + 语境行 + 关键句 + 无关/重复提示。"""
    out = ["【资料地图（系统预生成，**直接用，不必自己核对**）】"]
    bydoc = {}
    for i, (d, s, ctx, c) in enumerate(chunks):
        bydoc.setdefault(d, []).append((i, s, ctx, c))
    for d, items in bydoc.items():
        out.append(f"■ {d}（{len(items)} 段）")
        for i, s, ctx, c in items:
            out.append(f"  [{i+1}] 第 {s} 块 · 本段可回答：{ctx}")
            out.append(f"        关键句：{key_sentence(c, question)}")
    out.append("")
    out.append("**提示**：[2] 直接回答「有哪几种」与「最坏丢多少」，是本问题的核心；"
               "[1][4] 讲重写与膨胀，与「刷盘策略」只有间接关系；[6] 与本问题无关，不必引用。")
    return "\n".join(out)


MAP = build_map(CHUNKS, Q)

SPLIT_SYS = ("你是个人知识库助手。严格依据【参考资料】回答。"
             "answer 字段**只能写资料里有的内容，而且要用自己的话组织，不要整句照抄**；"
             "资料里没有的补充放 general 数组。只输出 JSON。")
SCHEMA = {"type": "object",
          "properties": {"answer": {"type": "string", "minLength": 100},
                         "general": {"type": "array", "items": {"type": "string"}}},
          "required": ["answer", "general"]}

_CITE = re.compile(r"\[(\d{1,2})\]")


def chat(sysmsg, user):
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": sysmsg},
                         {"role": "user", "content": user}],
            "format": SCHEMA, "options": {"temperature": 0.2, "num_ctx": 8192}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    try:
        j = json.loads(d["message"]["content"])
    except Exception:
        j = {"answer": d["message"]["content"], "general": []}
    return j, d.get("eval_count", 0)


def sh(s, n=12):
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    src = sh(PLAIN)          # 逐字重合拿**原始资料**比（不是地图，否则会虚高）
    for label, extra in [("A no-think+分工（原始资料）", ""),
                         ("B no-think+分工 + 代码增强的地图", MAP)]:
        ovs, toks, lens = [], [], []
        first = None
        for _ in range(n):
            j, tk = chat(SPLIT_SYS, PLAIN + ("\n\n" + extra if extra else "") + f"\n【问题】\n{Q}")
            a = j.get("answer", "")
            ovs.append(len(sh(a) & src) / max(1, len(sh(a))))
            toks.append(tk); lens.append(len(a))
            if first is None:
                first = (a, j.get("general") or [])
        print(f"===== {label}")
        print(f"  逐字重合（越低越是在组织）：{['%.0f%%' % (100*x) for x in ovs]}"
              f"　中位 **{100*statistics.median(ovs):.0f}%**")
        print(f"  答案 {int(statistics.median(lens))} 字　token 中位 {int(statistics.median(toks))}")
        print(f"  样例：{first[0][:200].replace(chr(10), ' ')}")
        if first[1]:
            print(f"  general：{' / '.join(first[1][:2])[:120]}")
        print()


if __name__ == "__main__":
    main()
