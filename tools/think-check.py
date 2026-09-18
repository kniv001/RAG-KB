# -*- coding: utf-8 -*-
"""
think 参数与 format 约束的交互 —— 今天所有 JSON 探针的共同前提，验一下。

causal-isolate-probe 的 F（回显）臂 0/8，输出是「首先，用户要求我"原样重复…"」——
**思考文本出现在 content 里**，说明 `think:false` 没生效。若真如此，那么加了 `format`
（JSON schema 语法约束）的调用里，第一个 token 必须是 `{`，思考就被语法整个禁掉了 ——
今天所有 JSON 探针测的都是「不许想、第一个 token 就得给答案」的模型。

本脚本三件事：
  1. 报 Ollama 版本（think 参数是较新版本才有）
  2. think False/True 各一次同一问题，打印 message 的**字段名**与两个字段的开头
  3. 四条哨兵句用**自然语言问法**（不加 format，允许思考），把 content **原样打出来** ——
     上一版 E 臂用「没有」做关键字，而模型复述问题时那句「有没有表示因果…」里就含「没有」，
     解析被污染；这里不解析，直接看文字。

用法：python tools/think-check.py
"""
import json
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
OPTS = {"temperature": 0.1, "num_ctx": 16384}

CASES = [
    ("因为缓存失效，所以请求全部落到了数据库上。", "因果"),
    ("这个异常是由于没有释放连接导致的。", "因果"),
    ("如果连接超时，就重试三次。", "条件"),
    ("这个函数返回一个整数。", "（反口：三类都不是）"),
]


def post(messages, fmt=None, think=False, timeout=180):
    body = {"model": CHAT, "stream": False, "think": think,
            "options": OPTS, "messages": messages}
    if fmt is not None:
        body["format"] = fmt
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def show(tag, d):
    m = d.get("message", {})
    print(f"  [{tag}] message 字段：{sorted(m.keys())}")
    th = m.get("thinking", "")
    ct = m.get("content", "")
    print(f"    thinking：{th[:120] if th else '（空 / 没有这个字段）'}")
    print(f"    content ：{ct[:120] if ct else '（空）'}")
    print(f"    content 长度 {len(ct)}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=10) as r:
            print("Ollama 版本：", json.load(r).get("version"))
    except Exception as e:
        print("版本查询失败：", e)
    print()

    q = [{"role": "user", "content": "13 乘 17 等于多少？只回答数字。"}]
    print("—— think 参数（无 format）——")
    show("think=False", post(q, think=False))
    show("think=True ", post(q, think=True))

    print("\n—— 加了 format 之后（think=False）——")
    show("format=schema", post(q, fmt={"type": "object",
                                       "properties": {"n": {"type": "integer"}},
                                       "required": ["n"]}, think=False))

    print("\n—— 自然语言问因果（无 format，允许思考）——")
    for sent, kind in CASES:
        print(f"\n  句子（期望 {kind}）：{sent}")
        d = post([{"role": "system", "content": "你是中文语法标注器。"},
                  {"role": "user", "content":
                   "下面这句话里有没有表示**因果**关系的连词（因为/所以/由于/因此/导致）？"
                   "只回答「有」或「没有」。\n\n句子：" + sent}], think=False)
        m = d.get("message", {})
        print(f"    thinking 长度 {len(m.get('thinking') or '')}")
        print("    content 原样 ↓")
        print("    " + "\n    ".join((m.get("content") or "(空)").split("\n")))


if __name__ == "__main__":
    main()
