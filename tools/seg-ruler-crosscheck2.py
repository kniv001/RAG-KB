# -*- coding: utf-8 -*-
"""
分段尺子的**第二标注者（读概率版）**。

第一版用 llama3.1:8b 生成式复核，结果是**同意率 73% / 虚报率 27%** —— 而且它不会说"我不确定"。
这一版换成本地判断器（`logprob-judge` 的机制）：逐位置问「这一段是不是一个新话题的开始？」
**读「是」「否」两个 token 的概率**，于是：

  · 有**置信度**可用（P 的远近就是把握）
  · 可以对**低置信度**的项单独统计 —— 那正是"该交给人"的那一批

三类样本（取自 `seg-ruler-annotation.json`）：
  · `must`（我标的**必切点**）→ "是"才对
  · `forbidden` 内部的位置      → "否"才对（**真反例**；上一版误把 optional 当反例，那是设计错误）
  · `optional`（可切点）        → **两边都不算错**，单独看它给多少

用法：python tools/seg-ruler-crosscheck2.py [模型，默认 qwen3:4b]
"""
import importlib.util
import io
import json
import math
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = os.environ.get("KB_JUDGE_MODEL", "qwen3:4b")
SEGS = os.path.join(HERE, "..", "data", "_bintree-segs.json")

FEWSHOT = (
    "前文：\n- 漏桶算法按照固定速率流出水滴。\n"
    "这一段：\n- 令牌桶算法按照固定速率往桶里放令牌，桶满了就丢弃。\n"
    "这一段是不是一个新话题的开始？\n答：否\n"
    "前文：\n- 令牌桶算法按照固定速率往桶里放令牌。\n"
    "这一段：\n- 四、使用 Semaphore 进行并发流控。Java 并发库的 Semaphore 可以控制同时访问的个数。\n"
    "这一段是不是一个新话题的开始？\n答：是\n")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def ask(prev, cur, topn=20, timeout=300):
    p = FEWSHOT + (f"前文：\n{prev}\n这一段：\n{cur}\n这一段是不是一个新话题的开始？\n答：")
    body = {"model": CHAT, "prompt": p, "raw": True, "stream": False, "think": False,
            "logprobs": True, "top_logprobs": topn,
            "options": {"temperature": 0, "num_predict": 1, "num_ctx": 4096}}
    req = urllib.request.Request(OLLAMA + "/api/generate",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    lp = (d.get("logprobs") or [{}])[0].get("top_logprobs") or []
    m = {}
    for t in lp:
        for k in {t["token"], t["token"].strip()}:
            m[k] = max(m.get(k, float("-inf")), t["logprob"])
    a, b = m.get("是"), m.get("否")
    return (math.exp(a) / (math.exp(a) + math.exp(b))) if (a is not None and b is not None) else None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    spec = importlib.util.spec_from_file_location("sr", os.path.join(HERE, "seg-ruler.py"))
    sr = importlib.util.module_from_spec(spec); spec.loader.exec_module(sr)
    ann = sr.load()
    segs = json.load(io.open(SEGS, encoding="utf-8"))["segs"]
    print(f"判断器 {CHAT}（读是/否概率）\n")

    stats = {"must": [0, 0, []], "forb": [0, 0, []], "opt": [0, 0, []]}
    for w in ann["windows"]:
        lo = w["lo"]
        for tag, items, want in (("must", w["must"], True),
                                 ("opt", w["optional"], None)):
            for it in items:
                i = it["at"]
                prev = "\n".join(f"- {segs[j].strip()[:90]}" for j in range(max(lo, i - 3), i))
                p = ask(prev or "（无）", f"- {segs[i-1].strip()[:120]}")
                if p is None:
                    continue
                got = p > 0.5
                if want is not None:
                    stats[tag][0] += (got == want)
                    stats[tag][1] += 1
                stats[tag][2].append(round(p, 3))
        # 真反例：禁切区**内部**的位置（不是它的边界）
        for f in w["forbidden"]:
            for i in range(f["lo"] + 1, f["hi"]):
                prev = "\n".join(f"- {segs[j].strip()[:90]}" for j in range(max(lo, i - 3), i))
                p = ask(prev or "（无）", f"- {segs[i-1].strip()[:120]}")
                if p is None:
                    continue
                stats["forb"][0] += (p <= 0.5)
                stats["forb"][1] += 1
                stats["forb"][2].append(round(p, 3))

    print(f"{'样本':<26}{'同意/正确':>12}{'均值 P(是)':>12}   分布")
    for tag, label in (("must", "必切点（应「是」）"), ("forb", "禁切区内部（应「否」）"),
                       ("opt", "可切点（两边都不算错）")):
        ok, n, ps = stats[tag]
        if n:
            print(f"{label:<26}{f'{ok}/{n} = {100*ok/n:.0f}%':>12}"
                  f"{sum(ps)/len(ps):>12.3f}   {sorted(ps)[:12]}")
    print("\n对照：llama3.1:8b 生成式复核是 同意率 73% / 虚报率 27%")
    hi = [p for p in stats["must"][2] if p >= 0.9]
    lo_ = [p for p in stats["forb"][2] if p <= 0.1]
    print(f"\n高置信部分：必切点里 P≥0.9 的 {len(hi)}/{len(stats['must'][2])}；"
          f"禁切里 P≤0.1 的 {len(lo_)}/{len(stats['forb'][2])}")


if __name__ == "__main__":
    main()
