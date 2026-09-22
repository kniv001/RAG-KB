# -*- coding: utf-8 -*-
"""
**多跑几次规划，拿多组查询** —— 给"功效来自重复而不是题数"这条路做供料。

背景（2026-09-22）：要判「语境行」（索引文本用 ctx+body / body / ctx 哪个好），
按题数算要写 **~148 道题**（产出率 27%，要 40 道有区分度的）—— 太贵。

而 `--source planner` 现在用的是**录下来的一次**规划结果，确定性 ⇒ 重复跑加不了方差。
**但如果重新跑 N 次规划**，每题就有 N 套不同查询 ⇒ **每题 N 个独立样本**。
25 题 × 10 次 = 250 样本，**不用写一道新题**。

**为什么可以本地跑，不用起应用**：单轮问答时规划那一步的输入就是
`用户问题：<q>`（无历史/摘要/召回），system 是固定的 `PLAN_PROMPT`，
schema 固定，temperature 0.2 ⇒ 本地照原样发一次就有同样的分布。
（多轮/带历史的题复现不了 —— 那些必须走真身。见 `AgenticRagService.plan()`。）

用法：python tools/plan-capture.py [次数，默认 10] [--bench multihop-25]
输出：tools/_plan-passes.json —— `[[{queries:[...]}, ...], ...]` 外层是"第几次"
"""
import io
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OLLAMA = "http://127.0.0.1:11434"
MODEL = os.environ.get("KB_PLAN_MODEL", "qwen3:4b")

# 与 `AgenticRagService.PLAN_PROMPT` / `PLAN_SCHEMA` **逐字相同** —— 不一样就不是同一个规划器了
PLAN_PROMPT = """你是知识库检索规划器。把用户的问题拆成 1~3 个用于检索的查询。

硬性要求：
1. 每个查询必须自包含，不能出现「它」「这个」「上面提到的」这类指代 —— 检索时没有对话上下文。
2. 查询用词要贴近资料里可能出现的说法，不要改写成抽象概念。
3. 问题简单就给 1 个查询，不要为了凑数硬拆。
4. 只输出 JSON，不要解释、不要加代码块标记。

输出格式：{"queries":["查询一","查询二"]}"""
PLAN_SCHEMA = ('{"type":"object","properties":{"queries":{"type":"array",'
               '"items":{"type":"string"}}},"required":["queries"]}')


def plan(question, timeout=180):
    body = {"model": MODEL, "stream": False, "think": False,
            "format": json.loads(PLAN_SCHEMA),
            "messages": [{"role": "system", "content": PLAN_PROMPT},
                         {"role": "user", "content": "用户问题：" + question}],
            "options": {"temperature": 0.2, "num_ctx": 16384}}
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = json.load(r).get("message", {}).get("content", "")
            qs = [str(x).strip() for x in (json.loads(txt).get("queries") or []) if str(x).strip()]
            # 规划失败时应用会降级成原问题 —— 这里照做，保持与真身同样的兜底
            return qs or [question]
        except Exception:
            time.sleep(2)
    return [question]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else 10
    bench = "multihop-25"
    if "--bench" in sys.argv:
        bench = sys.argv[sys.argv.index("--bench") + 1]

    from ruler import corpus, cases as Ccases
    C = corpus.load()
    cs = Ccases.load(bench, C)
    out_path = os.path.join(HERE, f"_plan-passes-{bench}.json")

    passes = json.load(io.open(out_path, encoding="utf-8")) if os.path.exists(out_path) else []
    print(f"已有 {len(passes)} 批　目标 {n} 批 × {len(cs.cases)} 题", flush=True)

    while len(passes) < n:
        row, t0 = [], time.time()
        for c in cs.cases:
            row.append({"queries": plan(c["q"])})
        passes.append(row)
        json.dump(passes, io.open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
        # **每批都落盘** —— 同 collect 的道理：中断了不用重跑前面的
        print(f"  第 {len(passes)} 批好了（{time.time()-t0:.0f}s）", flush=True)

    # 顺带报一下"同一题的查询在批次之间有多不一样" —— 方差够不够，先看这个
    diff = []
    for i in range(len(cs.cases)):
        sets = [set(p[i]["queries"]) for p in passes]
        uniq = len({tuple(sorted(s)) for s in sets})
        diff.append(uniq)
    same = sum(1 for d in diff if d == 1)
    print(f"\n{len(passes)} 批：完全一致的题 {same}/{len(diff)}　"
          f"有变化的题 {len(diff)-same}/{len(diff)}")
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
