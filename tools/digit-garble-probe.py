# -*- coding: utf-8 -*-
"""
数字被糊成「60:00」到底从哪来：输入里的冒号在串位吗？

事实（digit-fidelity-probe 已定）：
  · 隔离环境下**不坏** —— 纯回显/句中回显/压缩改写/变化式改写，19/20 逐字保住
  · 只有跑**完整的摘要提示词**时才坏，而且是**随机打滑**：
    同一轮里 600 既出现过「60:00 字」又出现过「600 字」

可疑点：输入里的分隔符全是冒号，而糊出来的东西**也是往数字中间插冒号**：
      已有摘要： / 用户： / 助手：
像不像注意力把「：」串进了数字里？那就把输入里的冒号去掉，其余一字不改，对打。

两组，每组 5 次（打滑是随机的，一次看不出来）：
  甲 原样：分隔符用「用户：」「助手：」
  乙 无冒号：「用户说」「助手说」，并且不再用「已有摘要：」这种写法

判据：输出里出现 **干净 600** 与 **糊掉的 600** 各几次。

用法：python tools/digit-garble-probe.py
"""
import importlib.util
import io
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
REPS = 5

# 直接借用 summary-shape-probe 里测过的那版提示词，保证测的是线上那条路
spec = importlib.util.spec_from_file_location("ssp", os.path.join(HERE, "summary-shape-probe.py"))
ssp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssp)
PROMPT, SCHEMA = ssp.DELTA_PROMPT, ssp.DELTA_SCHEMA

OLD = "用户在做个人 RAG 知识库；块大小定为 600 字；用户偏好表格而非选项式提问"
FRESH_COLON = ("用户：我想把块大小从 600 改成 450，因为有些块抽不出事实。\n"
               "助手：450 字边界会更碎，召回条数会变多。\n"
               "用户：另外记住，以后回答不要用「首先其次」这种套话。\n"
               "助手：明白，直接给结论。")
FRESH_PLAIN = ("用户说，我想把块大小从 600 改成 450，因为有些块抽不出事实。\n"
               "助手说，450 字边界会更碎，召回条数会变多。\n"
               "用户说，另外记住，以后回答不要用「首先其次」这种套话。\n"
               "助手说，明白，直接给结论。")

USER_COLON = f"已有摘要：\n{OLD}\n\n新增对话：\n{FRESH_COLON}"
USER_PLAIN = f"已有摘要如下。\n{OLD}\n\n新增对话如下。\n{FRESH_PLAIN}"

# 「600」被插了分隔符的几种形态
GARBLE = re.compile(r"6\s*[:：,.·、]\s*0\s*0|60\s*[:：,.·、]\s*0|6\s*[:：]\s*00")


def post(system, user, schema, timeout=180):
    body = {"model": CHAT, "stream": False, "think": False, "format": schema,
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
        return [str(x) for x in (json.loads(raw).get("items") or [])]
    except Exception:
        return [raw]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    stat = {}
    for tag, user in (("甲 原样（冒号分隔）", USER_COLON), ("乙 无冒号", USER_PLAIN)):
        clean = garbled = 0
        print(f"===== {tag} =====")
        for i in range(REPS):
            its = items_of(post(PROMPT, user, SCHEMA))
            joined = " ".join(its)
            has_clean = "600" in joined
            has_bad = bool(GARBLE.search(joined))
            clean += has_clean
            garbled += has_bad
            print(f"  #{i+1} 干净600 {'有' if has_clean else '无'}　糊 {'有' if has_bad else '无'}")
            for x in its:
                print(f"       · {x}")
        stat[tag] = (clean, garbled)
        print(flush=True)
    print("—— 5 次里出现几次 ——")
    print(f"{'条件':<18}{'干净的600':>10}{'糊掉的600':>10}")
    for k, (c, g) in stat.items():
        print(f"{k:<18}{c:>10}{g:>10}")


if __name__ == "__main__":
    main()
