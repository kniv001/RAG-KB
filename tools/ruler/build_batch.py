# -*- coding: utf-8 -*-
"""
**出题助手**：给「文档 + 块号 + 问句」，自动生成锚内容的靶子。

为什么要有它：靶子摘录必须是**与 `corpus.norm` 一致的 40 字**（`EXCERPT = 40`，别改 ——
换了长度 = 换了靶子组 = 数字与历史不可比）。手抄会不一致，而**不一致的靶子不报错**，
只会让全中率莫名变低 —— 正是本项目吃过好几次的那种坑。
`C.head(i)` 取的就是归一化正文的前 40 字，用它生成，天然与 `find()` 对得上。

用法（在 Python 里）：见 `SPEC` 的写法，`python tools/ruler/build_batch.py <输出名>`。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ruler import corpus  # noqa: E402

P = "实用指南：提示词工程方法及框架 - tlnshuju - 博客园.md"

# (编号, 文档, [块号...], 问句)
#
# 出题原则（从「为什么 72% 的题不贡献区分度」倒推出来的）：
#   · **要两块以上**，且它们**字面上离问句远** —— 一眼能匹配到的块不构成多跳
#   · 优先挑**文档里互相矛盾/互相补充**的两处（同一件事换个场景说法就反了）
#   · 跨度越大越好：跨小节 > 跨段 > 相邻
P2 = "高并发系统限流-漏桶算法和令牌桶算法 - 陶朱公Boy - 博客园.md"
P3 = "Redis持久化之RDB&AOF - Margaery - 博客园.md"
P4 = "浅谈微服务中的熔断,限流,降级 - 苹果芒 - 博客园.md"

# **第二批**：靶子由 `pair_finder.py` 选（互相似高、字面重叠低、同篇远距），
# 用来检验"按这个原则出题能不能把区分度产出率提到 28% 以上"。
SPEC2 = [
    ("b2-01", P2, [33, 36],
     "同样大小的桶、同样的速率，为什么令牌桶能一次性放行一批请求，而漏桶只能匀速放行？"
     "两个桶在「溢出」时的行为分别是什么？"),
    ("b2-02", P2, [2, 34],
     "文档把限流比作给接口装「保险丝」，那真的触发限流之后，它列出了哪几种处置方式？"
     "「排队」和「降级」各举了什么例子？"),
    ("b2-03", P3, [12, 20],
     "RDB 和 AOF 在「对主进程的 IO 负担」和「数据完整性」这两件事上正好互换 —— 两边分别怎么说？"
     "RDB 靠什么机制把 IO 从主进程挪走，代价又是什么？"),
    ("b2-04", P4, [2, 10],
     "文档说熔断是用来应对「雪崩效应」的；而它在讲重试时又强调「重试成本高的服务反而该少重试」——"
     "那个成本用什么量？另外在 A→B→C 这条链上，为什么最下游出问题反而最上游不用熔断？"),
    ("b2-05", P2, [34, 36],
     "文档把常见的限流按「限制什么」分了哪几类？每一类各举了哪个具体实现？"
     "而漏桶作为「计量工具」时，它控制的是流入还是流出？"),
    ("b2-06", P3, [12, 14],
     "RDB 启动和 AOF 启动谁的优先级高？RDB 恢复数据的效率比 AOF 高，可它为什么在数据完整性上仍然吃亏？"),
    ("b2-07", P4, [2, 10],
     "「服务熔断」和「服务降级」的区别是什么？双 11 那种场景降级具体会砍掉哪些功能？"
     "文档还提到限流的上游配置要依赖下游，这是为什么？"),
    ("b2-08", P2, [33, 34],
     "为什么说令牌桶「对用户友好」？文档讲限流目的时提到的三种处置，哪一种正是令牌桶擅长的？"),
]

SPEC = [
    ("pe01", P, [5, 45],
     "同是让模型做推理，文档对普通模型和 O1 系列的引导方式正好相反 —— 两边分别怎么说的？"
     "O1 为什么不需要这种引导？"),
    ("pe02", P, [11, 42],
     "给结构化提示选标记形式时，Claude 和 ChatGPT 各自更吃哪一套？"
     "这个差别在长法律文档那种任务上，用 XML 标签量化出来的收益是多少？"),
    ("pe03", P, [24, 25],
     "要让输出能直接被下游程序读，文档给了两条路 —— 一条靠 schema 约束，一条靠标记分隔。"
     "分别是什么？各举了文档里的什么例子？"),
    ("pe04", P, [24, 28],
     "提示里那类「先把话说死」的写法 —— 预设假设、硬性词、反幻觉约束 —— 各是为了防什么？"
     "文档各举了什么例子？"),
    ("pe05", P, [45, 46],
     "文档说 O1 系列并非适用于所有场景，那复杂任务该优先用什么？"
     "它和普通模型在提示写法上还有哪两条差别？"),
    ("pe06", P, [40, 42],
     "结构化标签用在法律长文档提取上，具体在哪些指标上有提升、提升多少？"
     "那个 focus 标签靠什么符号锁定条款类型？"),
    ("pe07", P, [14, 17],
     "把提示词拆成角色、上下文、指令、具体性这几块的那套框架叫什么？"
     "它对上下文那部分有什么硬要求？"),
]


def main():
    out_name = sys.argv[1] if len(sys.argv) > 1 else "batch1"
    C = corpus.load()
    index = {}
    for i in range(C.n):
        index.setdefault(C.doc[i], {})[str(C.seq[i])] = i

    spec = SPEC2 if out_name.startswith("batch2") else SPEC
    cases = []
    for cid, doc, seqs, q in spec:
        targets = []
        for s in seqs:
            i = index.get(doc, {}).get(str(s))
            if i is None:
                raise SystemExit(f"！{cid}：{doc[:20]} 里没有第 {s} 块")
            targets.append({"excerpt": C.head(i), "seq_hint": int(s)})
        cases.append({"id": cid, "doc": doc, "q": q, "targets": targets})

    out = {
        "schema": "ruler/cases@1",
        # **名字带 -batch**：这是待筛的候选池，不是正式题目集
        "name": out_name,
        "kind": "retrieval",
        "judge": "all-hit",
        "note": "待筛候选池：先 audit 保证靶子可解析，再按「三档索引文本下会不会翻」筛区分度。",
        "cases": cases,
    }
    p = os.path.join(TOOLS, "cases", f"{out_name}.json")
    json.dump(out, io.open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"写出 {len(cases)} 题 → {p}")
    for c in cases:
        print(f"  {c['id']}  {len(c['targets'])} 靶　{c['q'][:36]}")


if __name__ == "__main__":
    main()
