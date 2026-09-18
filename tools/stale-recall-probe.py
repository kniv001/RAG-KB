# -*- coding: utf-8 -*-
"""
旧事实被召回时会不会盖过新事实 —— 应用里「更早的对话」那一层的 D 组风险。

生产形态（读代码得来，不是猜的）：
  · 超出最近窗口的轮次 → 改写成**自包含笔记**（TurnDocService），再向量召回
  · 召回片段在提示词里是**纯行、无时间标记**（HistoryIndexService：一行一条，只有先后顺序）
  · 摘要那一层在最前，且现在是**变化式**（"600 字 → 450 字"）

所以真正要问的是：**摘要那层的信息，够不够救回被召回片段带来的陈旧值？**

场景：早前定下块大小 600，后来改成 450；两条都出窗口、都被召回。问「块大小是多少？」

四组，同一批输入，只改"给了什么"：
  A 无摘要 + 片段无标注   —— 最坏情况
  B 摘要(变化式) + 无标注 —— **现状**
  C 摘要(变化式) + 片段带「（较早）/（较晚）」
  D 无摘要 + 片段带标注   —— 单独量标注的效力

判据：答出 450（当前值）= 对；答 600 = **陈旧**。每组重复 3 次。

用法：python tools/stale-recall-probe.py
"""
import json
import re
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
REPS = 3

SYS = """你是这个项目的助手。只根据下面给你的材料回答，材料里没有的就直说没有。
直接给结论，再给一句依据。三句话以内。"""

SUMMARY = ("分块粒度：600 字 → 450 字（理由：有些块抽不出事实）\n"
           "重叠比例：— → 保持 10%")

OLD_NOTE = "分块粒度定为 600 字，块与块之间留 10% 重叠。"
NEW_NOTE = "分块粒度改为 450 字，重叠比例不变。"

REF = ("[1] 分块粒度影响召回的密度与覆盖：块越大，单块信息越完整但条数少；"
       "块越小则相反。经验上按文档类型在数百字量级取值。")

Q = "块大小现在是多少？"


def build(use_summary, mark):
    lines = []
    if use_summary:
        lines.append("【对话背景】（全部历史的摘要）\n" + SUMMARY + "\n")
    ex = (("（较早）" if mark else "") + OLD_NOTE + "\n"
          + ("（较晚）" if mark else "") + NEW_NOTE)
    lines.append("【更早的对话】（本次对话早前的内容，由检索召回）\n" + ex
                 + "\n（以上是本次对话早前说过的内容。）\n")
    lines.append("【参考资料】\n" + REF + "\n")
    lines.append("【问题】" + Q)
    return "\n".join(lines)


def ask(user):
    body = {"model": CHAT, "stream": False, "think": True,
            "options": {"temperature": 0.1, "num_ctx": 16384},
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": user}]}
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r).get("message", {}).get("content", "").strip()
        except Exception:
            time.sleep(3)
    return "<失败>"


CONDS = [("A 无摘要 + 无标注", False, False),
         ("B 摘要 + 无标注（现状）", True, False),
         ("C 摘要 + 带标注", True, True),
         ("D 无摘要 + 带标注", False, True)]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    stat = {}
    for tag, summ, mark in CONDS:
        ok = stale = 0
        print(f"===== {tag} =====")
        user = build(summ, mark)
        for i in range(REPS):
            ans = ask(user)
            has_new = "450" in ans
            has_old = bool(re.search(r"600", ans))
            if has_new and not has_old:
                ok += 1
            elif has_old and not has_new:
                stale += 1
            print(f"  #{i+1}  {'✅当前值' if has_new and not has_old else '❌陈旧' if has_old and not has_new else '⚠ 两者都提/都没提'}  {ans[:88]}")
        stat[tag] = (ok, stale)
        print(flush=True)
    print("—— 3 次里 ——")
    print(f"{'条件':<24}{'答当前值':>8}{'答陈旧值':>8}")
    for k, (o, s) in stat.items():
        print(f"{k:<24}{o:>8}{s:>8}")


if __name__ == "__main__":
    main()
