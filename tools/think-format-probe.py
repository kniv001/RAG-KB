# -*- coding: utf-8 -*-
"""
think:true 与 format（JSON 语法约束）能不能共存？

think-check.py 已定：think:false 时思考文本泄进 content；think:false + format 时
思考被语法整个禁掉（第一个 token 必须是 {）。于是今天所有 JSON 探针测的都是
「不许想、第一 token 就给答案」的模型 —— 「因果恒 0」就是这么来的。

本脚本回答一件事：**把 think 打开、format 留着**，语法约束是在思考之后才接管（⇒ 能救），
还是一开始就接管（⇒ format 与思考不可兼得，必须改成两步调用）。

用同一批哨兵（答案已知），五元问法，三个条件对照：
  A think:false + format   （先前基线，4/8）
  B think:true  + format   （本脚本重点）
  C think:true  + 无 format，改用「先自由作答、末尾一行给 JSON」的提示词（两步法的替代）

看 message.thinking 有没有内容：长度 0 ⇒ 语法一开始就接管。

用法：python tools/think-format-probe.py
"""
import json
import re
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
OPTS = {"temperature": 0.1, "num_ctx": 16384}

LABELS = ["疑问", "条件", "因果", "转折", "举例"]
DEFS = {
    "疑问": "在提问（问号、吗、呢、如何、是否）",
    "条件": "含条件从句（如果……则、一旦、当……时）",
    "因果": "讲因果（因为、所以、由于、因此、导致）",
    "转折": "含转折（但是、然而、不过、却）",
    "举例": "在举例（例如、比如、如下）",
}
SENTINELS = [
    ("因为缓存失效，所以请求全部落到了数据库上。", {"因果"}),
    ("这个异常是由于没有释放连接导致的。", {"因果"}),
    ("如果连接超时，就重试三次。", {"条件"}),
    ("但是这个方案的缺陷在于扩展性。", {"转折"}),
    ("例如，可以用哈希表把查找降到常数时间。", {"举例"}),
    ("内存为什么会一直涨？", {"疑问"}),
    ("这个函数返回一个整数。", set()),
]
PROMPT5 = ("对这句话逐类判断，五类都要给出答案（是/否）：\n\n"
           + "\n".join(f"- {L}：{DEFS[L]}" for L in LABELS)
           + '\n\n只输出 JSON：{"疑问":false,"条件":false,"因果":false,"转折":false,"举例":false}')
SCHEMA = {"type": "object",
          "properties": {k: {"type": "boolean"} for k in LABELS},
          "required": LABELS}


def post(messages, fmt=None, think=False, timeout=300):
    body = {"model": CHAT, "stream": False, "think": think,
            "options": OPTS, "messages": messages}
    if fmt is not None:
        body["format"] = fmt
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("message", {})


def parse_set(txt):
    try:
        o = json.loads(txt)
        return {L for L in LABELS if o.get(L) is True}
    except Exception:
        return None


def run(sent, think, fmt, tag):
    m = post([{"role": "system", "content": PROMPT5}, {"role": "user", "content": sent}],
             fmt=fmt, think=think)
    ct = (m.get("content") or "").strip()
    th = (m.get("thinking") or "")
    got = parse_set(ct)
    return th, got, ct


def fmt_set(s):
    return "全 false" if not s else " ".join(sorted(s))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"哨兵 {len(SENTINELS)} 条，同一五元问法，三个条件\n")
    t0 = time.time()
    score = {"A": 0, "B": 0}
    for sent, exp in SENTINELS:
        print(f"哨兵：{sent}")
        print(f"  期望：{fmt_set(exp) or '五类都不是'}")
        th, got, ct = run(sent, False, SCHEMA, "A")
        score["A"] += (got == exp)
        print(f"  A think:false + format   思考 {len(th)} 字　→ "
              f"{fmt_set(got) if got is not None else '解析失败'}")
        th, got, ct = run(sent, True, SCHEMA, "B")
        score["B"] += (got == exp)
        print(f"  B think:true  + format   思考 {len(th)} 字　→ "
              f"{fmt_set(got) if got is not None else '解析失败'}")
        if th:
            print(f"      （思考开头：{th.strip()[:70]}）")
        print(flush=True)
    n = len(SENTINELS)
    print(f"耗时 {time.time()-t0:.0f}s")
    print(f"  A think:false + format   {score['A']}/{n}")
    print(f"  B think:true  + format   {score['B']}/{n}")
    print("  思考字数恒 0 ⇒ 语法一开始就接管，format 与思考不可兼得（需改两步调用）")


if __name__ == "__main__":
    main()
