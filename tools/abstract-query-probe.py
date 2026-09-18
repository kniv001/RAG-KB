# -*- coding: utf-8 -*-
"""
抽象问句召回不到对症条目：索引里缺「跨条目的主题行」吗？

现象（见 2026-09-18-记忆当检索）：11 题里唯一答错的是「今天最贵的教训是什么」——
索引一行一条讲的是**每条决策自己**，而"教训"是**横跨多条**的东西（散在某个节点的
正文段落里）。抽象问句没有独有词，向量检索抓不住。

假设：索引里补几行**主题行**（跨条目的总结），这类问题就能答上。

两组，问题相同，只换索引文本：
  A 现状索引（tools/../decisions/personal-rag-kb/INDEX.md，26 行「曾→现」）
  B 现状索引 + 4 行主题行（教训 / 已否决 / 还活着 / 已知缺陷）

三个抽象问题，各重复 2 次（答案质量是读出来的，机械命中只作提示）：
  Q1 这次会话里最贵的教训是什么
  Q2 哪些方向已经被彻底否决了
  Q3 现在已知还没解决的问题有哪些

用法：python tools/abstract-query-probe.py
"""
import io
import json
import os
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = r"D:\vs\decisions\personal-rag-kb\INDEX.md"
REPS = 2

# 主题行：横跨多条、不属于任何单条节点的那种总结
TOPIC_LINES = """
**主题 · 教训**：任何「允许不做」的口子都会被模型用满（不切 / 凑数 / 全 false / 抄示例值）；
测量工具比结论更值得怀疑（`psql -A` 冲散多行 / 探针目标集为空 / harness 没送提示词 /
否定句让判据反转）；对照实验必须放哨兵。
**主题 · 已否决的方向**：KV 流式、DP 选块、命题改写、段检索+父块注入、结构化切分四条路、
检索单元自适应扩张、句间指向；结构化切分还额外被判定「失去需求适配」。
**主题 · 还活着的东西**：词语层指向（存活候选，触发条件是词面不匹配导致召不回）、
句子边界分块（已落地）、索引+召回的上下文形态（已验证）。
**主题 · 已知缺陷**：数字保真是 4b 解码层的值特异 artifact（提示词治不了，已加机械判据）；
摘要合并是双模态的（有兜底，但零星丢失挡不住）。
"""

QUESTIONS = [
    ("这次会话里最贵的教训是什么？", ["哨兵", "测量", "口子", "退化解"]),
    ("哪些方向已经被彻底否决了？", ["KV", "DP", "命题", "段检索", "结构"]),
    ("现在已知还没解决的问题有哪些？", ["数字", "合并", "覆盖边", "抽象", "丢"]),
]

SYS = """你是这个项目的助手。只根据下面给你的材料回答，材料里没有的就直说没有。
直接给结论，再给一句依据。四句话以内。"""


def post(body, timeout=300):
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("message", {}).get("content", "").strip()
        except Exception:
            time.sleep(3)
    return "<失败>"


def ask(q, index):
    return post({"model": CHAT, "stream": False, "think": True,
                 "options": {"temperature": 0.1, "num_ctx": 24576},
                 "messages": [{"role": "system", "content": SYS},
                              {"role": "user",
                               "content": "【项目记忆索引】\n" + index.strip()
                                          + "\n\n【问题】" + q}]})


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    base = io.open(INDEX, encoding="utf-8").read()
    conds = [("A 现状索引", base),
             ("B 索引 + 主题行", base + "\n" + TOPIC_LINES)]
    score = {}
    for tag, idx in conds:
        print(f"===== {tag}（{len(idx)} 字）=====")
        tot = 0
        for q, keys in QUESTIONS:
            print(f"  Q {q}")
            for i in range(REPS):
                ans = ask(q, idx)
                hit = sum(1 for k in keys if k in ans)
                tot += hit
                print(f"     #{i+1} 命中 {hit}/{len(keys)}  {ans[:150]}")
        score[tag] = tot
        print(flush=True)
    print("—— 机械命中合计（判定要读答案）——")
    for k, v in score.items():
        print(f"  {k:<18}{v}")


if __name__ == "__main__":
    main()
