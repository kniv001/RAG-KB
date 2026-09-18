# -*- coding: utf-8 -*-
"""
数字保真：模型把 600 写成「60:00」「60：」「60: 600」——是抄不对，还是写不对？

现象（会话摘要那条线，三种写法都出现）：
    段落式  "块大小为 60:00 字"
    状态式  "块大小定为 60：用户想改成 450 字"
    变化式  "块大小：60：600 → 450"
    另一处  "32K" → "3:2K"
共同点：**数字中间被塞进了冒号**，而且都是在"要改写/压缩"的环节。

四档任务，逐步放开（每档都换几个数字，看是不是和具体数字有关）：
  A 纯回显    ：「把下面这串字符原样写回来：600」
  B 句中回显  ：「把这句话原样写回来：块大小定为 600 字」
  C 压缩改写  ：「把这句话整理成一行条目（≤40 字）」  ← 生产里干的活
  D 变化式改写 ：旧摘要「块大小定为 600 字」+ 新增「改成 450」→ 按变化式输出

判据：输出里那串数字**是否逐字符等于原串**。A 就坏 = 词表/解码层问题；
A、B 好而 C、D 坏 = 压缩时的生成问题（那就得靠提示词或形状去治）。

用法：python tools/digit-fidelity-probe.py
"""
import json
import re
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
NUMBERS = ["600", "450", "1024", "32K", "9527"]

S = {"type": "object", "properties": {"out": {"type": "string"}}, "required": ["out"]}
SI = {"type": "object",
      "properties": {"items": {"type": "array", "items": {"type": "string"}}},
      "required": ["items"]}

SYS = "你是中文写作助手。严格按要求输出 JSON，不要解释。"


def post(system, user, schema, timeout=120):
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


def texts(raw):
    try:
        o = json.loads(raw)
    except Exception:
        return [raw]
    if "items" in o:
        return [str(x) for x in (o.get("items") or [])]
    return [str(o.get("out", ""))]


def check(out_texts, num):
    """返回 (是否逐字保住, 实际输出)"""
    joined = " ".join(out_texts)
    if num in joined:
        return True, joined
    # 找出被塞了分隔符的形态，便于读
    parts = "".join(num)
    pat = r".{0,3}".join(re.escape(c) for c in parts)
    m = re.search(pat, joined)
    return False, joined if not m else f"{joined}   ←误写：{m.group(0)}"


TASKS = [
    ("A 纯回显", S, lambda n: f"把下面这串字符原样写回来，一个字符都不要改：\n\n{n}",
     lambda o: "".join(o)),
    ("B 句中回显", S, lambda n: f"把下面这句话原样写回来，一个字都不要改：\n\n块大小定为 {n} 字。",
     lambda o: "".join(o)),
    ("C 压缩改写", SI, lambda n: f"把下面这句话整理成一行条目（不超过 40 字）：\n\n块大小定为 {n} 字。",
     lambda o: "".join(o)),
    ("D 变化式改写", SI,
     lambda n: (f"已有条目：\n块大小定为 {n} 字\n\n新增对话：\n用户：改成 450 吧。\n\n"
                f"按「曾经 → 现在」整理成一行条目，≤40 字。"),
     lambda o: "".join(o)),
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    total = ok = 0
    print(f"{'任务':<12}{'数字':>6}   结果")
    for name, schema, mk, _ in TASKS:
        for n in NUMBERS:
            raw = post(SYS, mk(n), schema)
            ts = texts(raw)
            good, shown = check(ts, n)
            total += 1
            ok += good
            flag = "✅" if good else "❌"
            print(f"{name:<12}{n:>6}   {flag}  {shown[:78]}")
        print(flush=True)
    print(f"\n逐字保住：{ok}/{total} = {100*ok/max(total,1):.0f}%")


if __name__ == "__main__":
    main()
