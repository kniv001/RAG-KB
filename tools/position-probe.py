# -*- coding: utf-8 -*-
"""
**位置到底有没有影响** —— 「三明治排序 ⑤」值不值得做，先问这一句。

背景：`rank()` 按距离升序排 ⇒ **最相关的块已经在开头**，最不相关的紧贴【问题】。
而代码注释里写着「【参考资料】是事实依据，让它紧挨着【问题】——『lost in the middle』
下这个位置最不容易被漏掉」。于是有个想法（休眠清单 ⑤）：把最相关的**放两头**。

**为什么不直接做 A/B**：答案侧基准已经**饱和**（10 次运行里 18/21 题恒过、
0 题恒挂，且没有任何一题靠近判据边缘）—— 在饱和的尺子上做 A/B，
**什么改动都会得到"没差别"**，包括真的有用的改动。

所以先花小钱问更根本的一句：**位置在这个模型上到底有没有影响？**
做法：同一题、同一批块，只换**顺序**，看答案变不变。

  A 原序（近→远，= 生产）      B 逆序（远→近，最相关的紧贴问题）
  C 三明治（最相关的放两头）

判据：① 三个答案彼此有多像（同不同）　② 各判据过不过　③ 引用了哪几块

**用法**：python tools/position-probe.py [题数，默认 6]
"""
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
MODEL = os.environ.get("KB_POS_MODEL", "qwen3:4b")
BASE_RUN = os.path.join(HERE, "eval", "_runs", "answer-quality__qwen3-4b.json")
sys.path.insert(0, HERE)


def answer_system():
    """从 Java 源码取 ANSWER_SYSTEM —— 与生产同一份，否则测的不是这个东西。"""
    p = os.path.join(os.path.dirname(HERE),
                     "rag-kb-service/src/main/java/com/kniv/ragkb/service/agent/AgenticRagService.java")
    s = io.open(p, encoding="utf-8").read()
    m = re.search(r'ANSWER_SYSTEM\s*=\s*"""(.*?)"""', s, re.S)
    return "\n".join(l.strip() for l in m.group(1).splitlines()) if m else ""


def body_of(C, s):
    i = next((k for k in range(C.n)
              if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
    return C.body[i] if i is not None else (s.get("preview") or "")


def render(C, srcs, order, q):
    """按给定顺序渲染【参考资料】—— **与应用的渲染逐字一致**（编号跟着顺序走）。"""
    lines = ["【参考资料】"]
    for k, idx in enumerate(order, 1):
        s = srcs[idx]
        lines.append(f"[{k}] 来源：{s['docName']}（第 {s['seq']} 块）")
        lines.append(body_of(C, s))
    lines.append("")
    lines.append("【问题】")
    lines.append(q)
    return "\n".join(lines)


def ask(system, user, timeout=600):
    body = {"model": MODEL, "stream": False, "think": True,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": 0.2, "num_ctx": 16384}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d.get("message", {}).get("content", "")


def shingles(s, n=12):
    t = re.sub(r"\s+", "", s or "")
    return {t[i:i + n] for i in range(len(t) - n + 1)}


ARMS = ("原序", "逆序", "三明治")


def orderings(n):
    base = list(range(n))
    rev = list(reversed(base))
    # 三明治：把最相关的两块摊到两头，其余按原序填中间
    if n >= 4:
        sand = [0] + base[2:] + [1]
    else:
        sand = base
    return {"原序": base, "逆序": rev, "三明治": sand}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n_q = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    from ruler import corpus
    C = corpus.load()
    sys.path.insert(0, os.path.join(HERE, "eval"))
    from eval import judges

    rows = json.load(io.open(BASE_RUN, encoding="utf-8"))["results"]
    rows = [x for x in rows if x["kind"] == "grounded" and len(x.get("sources") or []) >= 4][:n_q]
    SYS = answer_system()
    print(f"{len(rows)} 题 × {len(ARMS)} 种顺序 × 2 次（同序重跑，量固有噪声）\n")

    for x in rows:
        srcs = x["sources"]
        outs = {}
        for arm in ARMS:
            o = orderings(len(srcs))[arm]
            outs[arm] = [ask(SYS, render(C, srcs, o, x["q"])) for _ in range(2)]
        # ① 同序两次有多像 —— 这是**固有噪声底**，别把它当成顺序的影响
        base_sim = len(shingles(outs["原序"][0]) & shingles(outs["原序"][1])) / \
            max(1, len(shingles(outs["原序"][0]) | shingles(outs["原序"][1])))
        # ② 换序之后与原序有多像
        cross = {}
        for arm in ARMS[1:]:
            a = shingles(outs["原序"][0])
            bs = [shingles(outs[arm][i]) for i in range(2)]
            cross[arm] = max(len(a & b) / max(1, len(a | b)) for b in bs)
        print(f"  {x['q'][:34]}")
        print(f"    同序两次（噪声底）相似 {100*base_sim:.0f}%　"
              + "　".join(f"{arm} vs 原序 {100*v:.0f}%" for arm, v in cross.items()))
        # ③ 各判据过不过
        for arm in ARMS:
            j = judges.judge(x["kind"], outs[arm][0], srcs, x["q"])
            print(f"    {arm:<4} 逐字重合={j.get('逐字重合','—')}　"
                  f"最长照抄={j.get('最长照抄段','—')}　"
                  f"数字落地={j.get('数字落地','—')}")
        print()

    print("读法：把「换序相似度」与「同序噪声底」比 —— 若两者差不多，"
          "说明位置没有可测的影响，⑤ 不值得做。")


if __name__ == "__main__":
    main()
