# -*- coding: utf-8 -*-
"""
漏抽到底是不是可复现 —— 同一个块、同一套参数，跑两遍。

起因：前一轮 74 段里 53 段返回空数组（260ms），但受控对照里**同样参数、同样几个块**
11/11 全抽出来了。所以「漏抽」至少有一半是**跑级**现象，不是块级性质。

两种可能，后果完全不同：
  · 块级：某些块天生抽不出 → 可以靠前置过滤/重试解决，是局部问题
  · 跑级：跑到中途整批开始退化 → 批量重建时**静默坏掉一部分**，是最坏的失败模式
    （不报错、结果看起来正常、只是少了一大截）

判据：
  · 两遍的空集重合度 —— 高 = 块级；低 = 跑级
  · 空响应在运行中的位置分布 —— 集中在后半段 = 退化随运行累积
  · 每次调用的 prompt_eval_count / eval_count —— 空响应的 eval_count 只有几个 token，
    说明它确实没写东西；而 prompt_eval_count 正常则说明资料它是看到了的

用法：python tools/extract-repeat-probe.py
"""
import json
import os
import re
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_prop-probe.json")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_extract-repeat.json")

PROMPT = """把下面这段资料拆成若干条**自足的事实命题**。

硬性要求：
1. 每条命题必须脱离上下文就能读懂 —— 不许出现「它」「该」「此」「上述」「前者」这类指代，
   指代对象要写出来。
2. 只写资料里说过的事实。**不许补充资料之外的任何内容，不许推论**。
   资料里没有的数字、结论、因果关系一律不得出现。
3. 一条命题只讲一件事。同一条事实被反复说，只保留一次。
4. 保持原有术语与数字，不要改写数值。"""
SCHEMA = {"type": "object",
          "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
          "required": ["facts"]}


def post(path, body, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def extract(chunk):
    t0 = time.time()
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                           "options": {"temperature": 0.1, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": PROMPT},
                                        {"role": "user", "content": chunk}]})
    ms = int((time.time() - t0) * 1000)
    content = r.get("message", {}).get("content", "")
    facts = []
    try:
        facts = json.loads(content).get("facts", [])
    except Exception:
        m = re.search(r'"facts"\s*:\s*\[(.*?)\]', content, re.S)
        if m:
            facts = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    return {"n": len(facts), "ms": ms, "raw_len": len(content),
            "pe": r.get("prompt_eval_count"), "ped": r.get("prompt_eval_duration"),
            "ec": r.get("eval_count"), "ed": r.get("eval_duration"),
            "reason": r.get("done_reason"), "first": (facts[0][:40] if facts else "")}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    recs = json.load(open(DATA, encoding="utf-8"))["records"]
    chunks = [r["text"] for r in recs]
    print(f"同一批 {len(chunks)} 段，跑两遍\n")

    runs = []
    for run in (1, 2):
        res = []
        for i, c in enumerate(chunks, 1):
            d = extract(c)
            res.append(d)
            mark = "空" if d["n"] == 0 else f"{d['n']:>2}条"
            print(f"  [第{run}遍 {i:>2}/{len(chunks)}] {mark} {d['ms']:>5}ms "
                  f"pe={d['pe']} ec={d['ec']} {d['reason']}", flush=True)
        runs.append(res)
        empt = [i for i, d in enumerate(res, 1) if d["n"] == 0]
        print(f"  → 第{run}遍：空 {len(empt)}/{len(chunks)}　位置 {empt[:20]}\n")

    a, b = ({i for i, d in enumerate(r, 1) if d["n"] == 0} for r in runs)
    print("—— 结论判据 ——")
    print(f"第1遍空 {len(a)}，第2遍空 {len(b)}，两遍都空 {len(a & b)}，"
          f"至少一遍空 {len(a | b)}")
    print(f"空集重合度 Jaccard = {len(a & b)/max(len(a | b), 1):.2f}")
    print("  （高 = 块级：某些块天生抽不出；低 = 跑级：同一块时好时坏）")
    for run, res in enumerate(runs, 1):
        half = len(res) // 2
        e1 = sum(1 for d in res[:half] if d["n"] == 0)
        e2 = sum(1 for d in res[half:] if d["n"] == 0)
        print(f"  第{run}遍  前半空 {e1}/{half}　后半空 {e2}/{len(res)-half}")
    empt = [d for r in runs for d in r if d["n"] == 0]
    if empt:
        print(f"  空响应的 eval_count：{sorted({d['ec'] for d in empt})[:10]}"
              f"（真抽取是几十到几百）")
        print(f"  空响应的 prompt_eval_count 中位数："
              f"{sorted(d['pe'] or 0 for d in empt)[len(empt)//2]}")
    ok = [d for r in runs for d in r if d["n"] > 0]
    if ok:
        print(f"  正常响应的 eval_count 中位数：{sorted(d['ec'] or 0 for d in ok)[len(ok)//2]}")
    json.dump({"runs": runs}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
