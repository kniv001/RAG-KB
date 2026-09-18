# -*- coding: utf-8 -*-
"""
数字修复的端到端：**规则在纸面上没错，不代表在模型的真实输出上没错**。

纸面（digit-repair-rule-probe）12/12，用的是手写的糊法。这里让模型真的糊：
跑**线上那条完整摘要提示词**，输入里塞三个已知中招的值（600 / 3000 / 24576），
把原始输出与修复后的输出并排打出来 —— **判定要读，不能只看计数**。

判据：
  · 修复前：几个条目里出现糊掉的形态
  · 修复后：还有没有残留；**以及有没有把没糊的东西改坏**

用法：python tools/digit-repair-e2e-probe.py [次数，默认 6]
"""
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ssp = load("summary-shape-probe")          # 线上提示词（变化式）
drr = load("digit-repair-rule-probe")      # 待验的修复规则
dgp = load("digit-garble-probe")           # 台账里记的那个**真实触发例**

PROMPT, SCHEMA = ssp.DELTA_PROMPT, ssp.DELTA_SCHEMA

# 用触发例原文（第一版自己编的输入有歧义：模型把「上限 3000 条」和「窗口上限 24576」
# 混成一条，反而一次都没糊 —— 那不是没病，是没扎到穴位）
OLD = dgp.OLD
FRESH = dgp.FRESH_COLON
SOURCE = OLD + "\n" + FRESH
SUSPECT = re.compile(r"\d{2,}\s*[:：]\s*(?!\d)")


def post(system, user, timeout=180):
    body = {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.2, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("message", {}).get("content", "")
        except Exception:
            time.sleep(2)
    return ""


def items_of(raw):
    try:
        return [str(x).strip() for x in (json.loads(raw).get("items") or []) if str(x).strip()]
    except Exception:
        return []


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    user = f"已有条目：\n{OLD}\n\n新增对话：\n{FRESH}"
    n_before = n_after = n_fixed = 0
    changed_cases = 0
    for i in range(1, reps + 1):
        items = items_of(post(PROMPT, user))
        fixed = drr.repair(items, SOURCE)
        before = [it for it in items if SUSPECT.search(it)]
        after = [it for it in fixed if SUSPECT.search(it)]
        n_before += len(before)
        n_after += len(after)
        n_fixed += len(before) - len(after)
        print(f"—— 第 {i} 次 ——")
        for a, b in zip(items, fixed):
            mark = "  " if a == b else "→ "
            print(f"  {mark}{a}")
            if a != b:
                print(f"     {b}")
                changed_cases += 1
        print()
        sys.stdout.flush()
    print(f"修复前可疑形态 {n_before} 处，修复后剩 {n_after} 处，动了 {changed_cases} 条")
    print("（判定看上面每一对：修对的应该是「末位被冒号顶掉」；若动到时间/比例就是误伤）")


if __name__ == "__main__":
    main()
