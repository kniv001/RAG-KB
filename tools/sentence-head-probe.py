# -*- coding: utf-8 -*-
"""
用户提案：**逐句增量输入，问每一句的主旨句是几号**（本句就是主旨就填本句的编号）。
设想是"选择多元化" —— 每句的候选集不同，就不容易像二选一那样坍塌成一个常数。

两个设计要点（都是今天踩出来的）：
  ① **"本句"不设特殊 token**：若答 0 表示"本句"，那 0 就是个新出口，会被用满（换一个常数）。
     这里本句就填**它自己的编号**，答案空间永远是"任意句号"，提示词里也没有可抄的字面值。
  ② 必须带**对照**：一段"有主旨结构"的文字（答案该是 1,1,1,1,5,5,5,5）与一段
     "互不相干"的文字（答案该全是自己）。只看真文档的话，坍塌与正确分不开。

同时量一条**零成本基线**：机械地答"前一句"。它若打平或赢过模型，这条线就不值当。

输入方式照提案：**前缀逐步增长**，每次只问最后那一句。

用法：python tools/sentence-head-probe.py [--real]
"""
import io
import json
import os
import re
import sys
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = os.environ.get("KB_MODEL", "qwen3:4b")
_NG = int(os.environ["KB_NUM_GPU"]) if os.environ.get("KB_NUM_GPU") else None


def _opts(temp, ctx):
    o = {"temperature": temp, "num_ctx": int(os.environ.get("KB_NUM_CTX", ctx))}
    if _NG is not None:
        o["num_gpu"] = _NG
    return o


# 提示词里**不能出现任何具体编号**（今天实测：写了 {"at": 3} 就五次全答 3）
PROMPT = """下面是一段文字，按句子编号。请判断**最后那一句**的主旨句是第几句。

规则：
- 如果最后这句是在**展开**前面某一句，就填那一句的编号
- 如果最后这句**自己就是**它所在段落的主旨句（在引出话题，不是展开别人），
  就填**它自己的编号**
- 只回答最后那一句，不要回答别的句子
- 不要解释"""

SCHEMA = {"type": "object", "properties": {"head": {"type": "integer"}}, "required": ["head"]}

# v2：把「主旨句」这个抽象词换成**可操作的定义**（与能跑通的叶子切分提示词同一风格）。
# v1 实测：模型把它当成答不出来的问题，15/16 次填 0（0 不在提示词里，是它自己的空值）。
PROMPT_V2 = """下面是一段文字，按句子编号。

任务：为**编号最大的那一句**找一个编号 —— 它和前面哪一句**讲的是同一件事**？

- 「同一件事」= 说的是同一个对象、同一个结论，只是换了说法，或者在补充它的细节
- 找到了就填那一句的编号
- 如果它**开始讲另一件事**（换了对象或换了结论），就填**它自己的编号**
- 输出只有那个编号，不要写别的句子
- 不要解释"""

# v3：v2 去掉一处歧义 —— 原句「只回答最后那一句」可以读成"把最后那句回答出来"，
# 模型可能因此不把它当判断题（实测 v1/v2 都在填 0）。
PROMPT_V3 = PROMPT_V2.replace("- 输出只有那个编号，不要写别的句子",
                              "- 只在 JSON 里填那个编号，不要输出任何句子原文")

if os.environ.get("KB_HEAD_V") == "3":
    PROMPT = PROMPT_V3
elif os.environ.get("KB_HEAD_V") == "2":
    PROMPT = PROMPT_V2


# —— 对照甲：两段各"1 句主旨 + 3 句展开"，答案应当是 自己/1/1/1/自己/5/5/5 ——
CTRL_STRUCTURED = [
    "检索质量这件事，可以拆成三个互相独立的环节来看。",              # 1 主旨
    "第一环是切分，块里混着半句话时，向量就代表不了它。",              # 2 → 1
    "第二环是表述，文档用机制词、用户用症状词，词面就对不上。",          # 3 → 1
    "第三环才是排序，前面的环节没做好时，排序再好也救不回来。",          # 4 → 1
    "后台任务在这台机器上有一个绕不开的约束：只有一个推理槽。",          # 5 主旨
    "所以后台任务必须等用户静默下来才能开始跑。",                    # 6 → 5
    "等不到就放弃这一次，因为那些活都是增量的。",                    # 7 → 5
    "否则用户的问题就得排在它后面干等。",                          # 8 → 5
]
CTRL_STRUCTURED_WANT = [1, 1, 1, 1, 5, 5, 5, 5]

# —— 对照乙：八句互不相干（各来自不同文档），答案应当全是"自己" ——
CTRL_UNRELATED = [
    "Redis 的 RDB 是某一时刻的全量快照。",
    "Kubernetes 调度器先过滤再打分。",
    "Rust 的所有权在编译期检查。",
    "布隆过滤器用多个哈希位判断存在性。",
    "G1 把堆划分成大小相等的 Region。",
    "DNS 递归解析从根域开始逐级下问。",
    "MVCC 靠版本链和 Read View 实现隔离。",
    "一致性哈希用虚拟节点抹平倾斜。",
]


def ask(prefix, timeout=300):
    body = {"model": CHAT, "stream": False, "think": os.environ.get("KB_THINK") == "1",
            "format": SCHEMA, "options": _opts(0.1, 8192),
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": "\n".join(
                             f"{i+1}. {s}" for i, s in enumerate(prefix))}]}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.load(r).get("message", {}).get("content", "")
    try:
        v = json.loads(raw).get("head")
        return int(v) if isinstance(v, (int, float)) else -1
    except Exception:
        return -1


def run(name, sents, reps=2):
    print(f"—— {name} ——")
    got = []
    for rep in range(reps):
        row = [ask(sents[:i + 1]) for i in range(len(sents))]
        got.append(row)
        print(f"  第{rep+1}次： {' '.join(f'{i+1}→{v}' for i, v in enumerate(row))}")
        sys.stdout.flush()
    dist = Counter(v for row in got for v in row)
    print(f"  答案分布：{dict(sorted(dist.items()))}"
          f"　{'⚠️ 常数' if len(dist) == 1 else ''}")
    return got


def score(got, want):
    n = sum(1 for row in got for a, b in zip(row, want) if a == b)
    tot = sum(len(row) for row in got)
    prev = sum(1 for row in got for i, a in enumerate(row) if a == i)   # 机械基线：答"前一句"
    self_ = sum(1 for row in got for i, a in enumerate(row) if a == i + 1)
    print(f"  命中 {n}/{tot}　｜　零成本基线「答前一句」{prev}/{tot}　"
          f"「答本句」{self_}/{tot}")
    return n, tot


def count_check():
    """仪器自检：一个平凡问题（最后一句的编号是几）。它都答不对 ⇒ 是编号/格式的锅，不是任务难。"""
    print("—— 对照丙：计数（仪器自检，平凡问题）——")
    p = """下面是一段文字，按句子编号。

请回答：**最后那一句的编号是几**？

只输出 JSON，字段名 num，值是那个编号。不要解释。"""
    for n in (3, 6):
        sents = CTRL_STRUCTURED[:n]
        body = {"model": CHAT, "stream": False, "think": False,
                "format": {"type": "object", "properties": {"num": {"type": "integer"}},
                           "required": ["num"]},
                "options": _opts(0.1, 8192),
                "messages": [{"role": "system", "content": p},
                             {"role": "user", "content": "\n".join(
                                 f"{i+1}. {s}" for i, s in enumerate(sents))}]}
        req = urllib.request.Request(OLLAMA + "/api/chat",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = json.load(r).get("message", {}).get("content", "")
        try:
            got = int(json.loads(raw).get("num"))
        except Exception:
            got = None
        print(f"  前缀 {n} 句 → 答 {got}（期望 {n}）{'✅' if got == n else '❌'}"
              f"　原始 {raw[:60]}")
        sys.stdout.flush()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"模型 {CHAT}　think={os.environ.get('KB_THINK') == '1'}　逐句前缀输入\n")

    count_check()
    print()
    g1 = run("对照甲：两段各「1 主旨 + 3 展开」", CTRL_STRUCTURED)
    s1 = score(g1, CTRL_STRUCTURED_WANT)
    print()
    g2 = run("对照乙：八句互不相干（各来自不同文档）", CTRL_UNRELATED)
    want2 = list(range(1, 9))
    s2 = score(g2, want2)
    print("\n  读法：对照甲看它能不能**跟着结构走**（答案该是 1,1,1,1,5,5,5,5）；")
    print("        对照乙看它会不会**乱指**（该全是本句）。两个都过，才轮到真文档。")

    if "--real" in sys.argv:
        cache = os.path.join(HERE, "..", "data", "_bintree-segs.json")
        if not os.path.exists(cache):
            print("\n（缺 data/_bintree-segs.json）")
            return
        segs = json.load(io.open(cache, encoding="utf-8"))["segs"]
        start = int(sys.argv[sys.argv.index("--real") + 1]) if len(sys.argv) > sys.argv.index("--real") + 1 else 0
        win = segs[start:start + 8]
        print(f"\n—— 真文档 段{start+1}~{start+len(win)} ——")
        row = [ask(win[:i + 1]) for i in range(len(win))]
        for i, (s, a) in enumerate(zip(win, row)):
            print(f"  {i+1}→{a}　{s[:70]}")
        print(f"  分布 {dict(sorted(Counter(row).items()))}")


if __name__ == "__main__":
    main()
