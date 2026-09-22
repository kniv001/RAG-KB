# -*- coding: utf-8 -*-
"""
**能不能把文字直接注入到「思考」里**（assistant prefill / 续写）。

想法（用户提的）：思考的大头是**同一件事换措辞说十几遍**，而两段式失败在
"第二段仍然被允许重新开始想"。如果**直接把一段开头塞进思考**，模型只能从那里往下写 ——
它**没有"重新开始"的机会**。而且注入的是**改写**而非新信息，所以不改变它看到的内容。

要验三件事（缺一不可）：
  ① 尾随一条 `assistant` 消息，模型会不会**续写**（而不是忽略、或当成完整回答）
  ② 前缀里写 ` thinking` 时，续写内容落进 **thinking 通道**还是 content 通道
     —— 落错通道就等于把"思考"混进了正文（本项目明令禁止）
  ③ 不写 ` thinking` 时是什么行为（对照）

用法：python tools/think-prefill-probe.py
"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _artifact import save                                    # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"


def chat(messages, think=None, timeout=180):
    body = {"model": MODEL, "stream": False, "messages": messages,
            "options": {"temperature": 0.2, "num_ctx": 8192}}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


SYS = "你是知识库助手。先用思考理清要点，再写回答。"
USR = ("参考资料：\nRDB 是某一时刻的全量快照，AOF 记录每一条写命令。\n"
       "问题：Redis 重启时会用哪个文件恢复？")

CASES = [
    ("① 纯续写（无前缀，assistant 只有一句开场）",
     [{"role": "system", "content": SYS}, {"role": "user", "content": USR},
      {"role": "assistant", "content": "要点一：RDB 是快照。\n要点二："}]),
    ("② 前缀以  thinking 开头（想注入进思考通道）",
     [{"role": "system", "content": SYS}, {"role": "user", "content": USR},
      {"role": "assistant", "content": " thinking\n分析：1. 资料讲了 RDB 快照 2. "}]),
    ("③ 前缀以  thinking 开头且已闭合（应该走正文）",
     [{"role": "system", "content": SYS}, {"role": "user", "content": USR},
      {"role": "assistant", "content": " thinking\n分析完毕。<｜end▁of▁thinking｜>\n回答："}]),
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    seen = []
    for name, msgs in CASES:
        print(f"===== {name}")
        try:
            d = chat(msgs)
        except Exception as e:
            print(f"  ✗ 请求失败：{e}\n")
            continue
        m = d.get("message", {})
        th, ct = m.get("thinking", "") or "", m.get("content", "") or ""
        # **屏幕上只打节选，完整的两份另存** —— 这道探针的全部意义就是"续写落在哪个
        # 通道、续了什么"，而 120 字的节选答不了后半句。节选是给终端看的，
        # 回溯要的是全文。（没走 `tools/run.py` 包时会写到临时目录并把路径打出来。）
        n = len(seen)
        seen.append(name)
        save(f"{n:02d}-thinking.txt", th)
        save(f"{n:02d}-content.txt", ct)
        print(f"  thinking 通道 {len(th):>5} 字：{th[:120]!r}")
        print(f"  content  通道 {len(ct):>5} 字：{ct[:160]!r}")
        print(f"  （全文已存：{n:02d}-thinking.txt / {n:02d}-content.txt，见本次运行目录）")
        print(f"  ⇒ 续写落在：{'**thinking** ✅' if len(th) > len(ct) else 'content'}"
              f"　（prompt {d.get('prompt_eval_count')} tok / gen {d.get('eval_count')} tok）\n")


if __name__ == "__main__":
    main()
