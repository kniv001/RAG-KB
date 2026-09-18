# -*- coding: utf-8 -*-
"""
摘要条数上限（现在拍的 12 条）到底该是多少：**按累积测，不按单次测**。

为什么这样测：摘要不是一次成型的 —— 每攒 6 条消息就把新内容并进已有条目（增量合并）。
所以上限的真正后果是**旧事实被新事实挤掉**（记忆会忘），而不是"单次压得紧不紧"。
单次测试看不见这一点。

做法：三轮对话，每轮都做一次真实合并（用线上那版变化式提示词），上限分别取 6 / 12 / 20。
第一轮放 8 条事实，后两轮持续加新事实 + 改掉其中一条（考察覆盖边是否也吃条目数）。

判据：**第一轮那 8 条事实活到最后还剩几条**（关键词机械检查 + 打印全文供读）。
另记最终条目数与提示词体积 —— 上限越高，常驻的代价越大。

用法：python tools/summary-cap-probe.py
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"

spec = importlib.util.spec_from_file_location("ssp", os.path.join(HERE, "summary-shape-probe.py"))
ssp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssp)
BASE = ssp.DELTA_PROMPT
SCHEMA = ssp.DELTA_SCHEMA

# 第一轮的 8 条事实：偏好、背景、数字混着来（关键词用于机械核对）
ROUND1 = [
    ("用户偏好表格而非选项式提问", "表格"),
    ("项目用 PostgreSQL + pgvector", "pgvector"),
    ("分块粒度定为 600 字", "600"),
    ("提示词窗口上限取 16384", "16384"),
    ("用户要求回答不要用「首先其次」这类套话", "首先其次"),
    ("语料现有 812 段", "812"),
    ("主题树一共 11 个簇", "11"),
    ("对话模型用 qwen3:4b", "qwen3"),
]
R1_TURNS = ("用户：几个背景，记一下 —— 偏好表格而不是选项式；库是 PostgreSQL + pgvector；"
            "分块粒度定 600 字；窗口上限取 16384；回答别用「首先其次」这种套话。\n"
            "助手：记下了。另外补充：语料现在 812 段，主题树 11 个簇，对话模型是 qwen3:4b。")
R2_TURNS = ("用户：分块粒度改成 450 吧，600 有些块抽不出事实。\n"
            "助手：好。另外今天加了条规矩：召回片段要带来源标注。\n"
            "用户：还有个坑，psql -A 会把多行内容冲散，得用 COPY。")
R3_TURNS = ("用户：抓取那边改成保留 Markdown 结构了，只对新文档生效。\n"
            "助手：明白。另外摘要上限这事我先试 12 条。\n"
            "用户：最后一条，写台账时文件名别带空格，git add 会被拆断。")
ROUNDS = [("第一轮", "", R1_TURNS), ("第二轮", None, R2_TURNS), ("第三轮", None, R3_TURNS)]


def ask(prompt, old, fresh):
    user = f"已有摘要：\n{old or '（无，这是第一次）'}\n\n新增对话：\n{fresh}"
    body = {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.2, "num_ctx": 8192},
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": user}]}
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            raw = d.get("message", {}).get("content", "")
            items = [str(x) for x in (json.loads(raw).get("items") or [])]
            return items, int(d.get("prompt_eval_count") or 0)
        except Exception:
            time.sleep(2)
    return [], 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for cap in (6, 12, 20):
        prompt = BASE.replace("7. 最多 12 条，每条不超过 40 字。",
                              f"7. 最多 {cap} 条，每条不超过 40 字。")
        assert f"最多 {cap} 条" in prompt
        cur, tok = "", 0
        print(f"===== 上限 {cap} 条 =====")
        for name, _, turns in ROUNDS:
            items, tok = ask(prompt, cur, turns)
            cur = "\n".join(items)
            print(f"  {name}：{len(items)} 条　提示词 {tok} token")
            for x in items:
                print(f"      · {x}")
        joined = cur
        alive = [k for _, k in ROUND1 if k in joined]
        dead = [k for txt, k in ROUND1 if k not in joined]
        print(f"  第一轮 8 条事实活到第三轮：{len(alive)}/8")
        if dead:
            print(f"  丢掉的：{dead}")
        print(flush=True)


if __name__ == "__main__":
    main()
