# -*- coding: utf-8 -*-
"""
治抽取退化：受控对照，找出哪个开关决定「真抽」还是「走逃生口」。

现象：74 段里 53 段返回 {"facts":[]}，而且只花 260ms（真抽取要 3.4~3.8 秒）。
模型根本没读资料。加一句「没有事实就返回空数组」的逃生口会让情况从 47% 恶化到 97%，
说明它对「允许不做」极度敏感 —— 而 format 语法约束下最短的合法输出正是 {"facts":[]}。

所以怀疑集中在三处：语法约束（format）、思考开关（think）、以及提示词里有没有下限要求。
本脚本只改这三处，在同一批料上对打，别的一律不动。

用法：python tools/extract-fix-probe.py
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

BASE_PROMPT = """把下面这段资料拆成若干条**自足的事实命题**。

硬性要求：
1. 每条命题必须脱离上下文就能读懂 —— 不许出现「它」「该」「此」「上述」「前者」这类指代，
   指代对象要写出来。
2. 只写资料里说过的事实。**不许补充资料之外的任何内容，不许推论**。
   资料里没有的数字、结论、因果关系一律不得出现。
3. 一条命题只讲一件事。同一条事实被反复说，只保留一次。
4. 保持原有术语与数字，不要改写数值。"""

MIN_RULE = """
5. **至少要输出 3 条命题**。这段资料是从技术文档里切出来的，一定有可提取的内容；
   若你觉得没什么可写，说明标准定得太高了 —— 放宽到「资料里出现过的任何具体陈述」都要写。"""

SCHEMA = {"type": "object",
          "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
          "required": ["facts"]}


def post(path, body, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_facts(txt):
    if not txt:
        return []
    try:
        return json.loads(txt).get("facts", [])
    except Exception:
        m = re.search(r'"facts"\s*:\s*\[(.*?)\]', txt, re.S)
        if m:
            return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        m = re.search(r"\[.*\]", txt, re.S)
        if m:
            try:
                return [x for x in json.loads(m.group(0)) if isinstance(x, str)]
            except Exception:
                pass
    return []


def run(chunk, variant):
    body = {"model": CHAT, "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 8192},
            "messages": [{"role": "system", "content": variant["prompt"]},
                         {"role": "user", "content": chunk}]}
    body["think"] = variant["think"]
    if variant["format"]:
        body["format"] = SCHEMA
    t0 = time.time()
    r = post("/api/chat", body)
    ms = int((time.time() - t0) * 1000)
    msg = r.get("message", {})
    facts = parse_facts(msg.get("content", ""))
    return facts, ms, (msg.get("thinking") or "")


VARIANTS = [
    {"name": "V0 现状", "think": False, "format": True, "prompt": BASE_PROMPT},
    {"name": "V1 去format", "think": False, "format": False, "prompt": BASE_PROMPT},
    {"name": "V2 开think", "think": True, "format": True, "prompt": BASE_PROMPT},
    {"name": "V3 加下限", "think": False, "format": True, "prompt": BASE_PROMPT + MIN_RULE},
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    recs = json.load(open(DATA, encoding="utf-8"))["records"]
    dead = [r for r in recs if not r["facts"]]
    live = [r for r in recs if r["facts"]]
    # 抽不出来的取 8 段（按长度取中位附近，避开极短极长），抽得出来的取 3 段作对照
    dead = sorted(dead, key=lambda r: len(r["text"]))
    dead = dead[len(dead) // 4: len(dead) // 4 + 8]
    live = sorted(live, key=lambda r: len(r["text"]))[:3]
    sample = [("抽不出", r) for r in dead] + [("抽得出", r) for r in live]
    print(f"样本：抽不出的 {len(dead)} 段 + 抽得出的 {len(live)} 段\n")

    tally = {v["name"]: {"ok": 0, "n": 0, "facts": 0} for v in VARIANTS}
    print(f"{'组':<7}{'段落':<26}" + "".join(f"{v['name']:>14}" for v in VARIANTS))
    for tag, r in sample:
        row = []
        for v in VARIANTS:
            facts, ms, think = run(r["text"], v)
            tally[v["name"]]["n"] += 1
            tally[v["name"]]["facts"] += len(facts)
            if facts:
                tally[v["name"]]["ok"] += 1
            row.append(f"{len(facts):>4}条/{ms:>4}ms")
        print(f"{tag:<7}{r['text'][:24]:<26}" + "".join(f"{c:>14}" for c in row), flush=True)

    print()
    for v in VARIANTS:
        t = tally[v["name"]]
        print(f"  {v['name']:<10} 有命题 {t['ok']}/{t['n']}　"
              f"共 {t['facts']} 条　平均 {t['facts']/max(t['n'],1):.1f} 条/段")


if __name__ == "__main__":
    main()
