# -*- coding: utf-8 -*-
"""
结构化切分：让模型对**整篇文档**输出层级边界（仍然不做理解、不改写）。

与平面切分的区别在哪：
  · 平面切分是**逐块**做的（600 字段落 → 2~4 段），所以「父节点」只能是那个机械块
  · 结构化切分是**逐文档**做的：文档 → 章 → 节，边界全由模型定，文本仍是逐字原文
  · 于是「段检索 → 注入父节点」里的父节点是**语义单元**，不再是打包产物

这正是前面被否掉的那个假设的**另一个版本**：我之前否的是「段 + 机械 600 字父块」，
那是两回事。先说清楚：本脚本只验**第一步 —— 模型能不能产出合法的层级**，
能不能带来收益要等这一步通过之后再测。

输出形状（故意选最简单的两级，便于机械校验）：
  {"sections":[{"span":[1,14],"breaks":[6,11]}, {"span":[15,30],"breaks":[]}]}
  含义：第 1~14 句为一章，章内在第 6、11 句后断开 → 三节；
        第 15~30 句为另一章，不再细分。
  机械校验：各章 span 必须**首尾相接、覆盖 1..N 不重不漏**；breaks 必须落在章内且递增。

用法：python tools/hier-segment-probe.py [文档数，默认 4]
"""
import io
import json
import os
import re
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
STORAGE = r"D:\vs\rag-kb\data\uploads"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data", "_hier.json")

PROMPT = """下面是一篇文档，已按句子编号（1 到 N）。请按**结构**把它切成章，章内再切成节。

只输出 JSON：{"sections":[{"span":[起,止],"breaks":[断点...]}]}

要求：
- 各章的 span 必须**首尾相接**：第一章从 1 开始，下一章从上一章的止+1 开始，最后一章止于 N
- breaks 是**本章内部**的断点句号，递增、且都落在本章 span 之内；不细分就写空数组
- 章的划分依据是主题转换（换了在讲什么），不是长度。章通常 8~40 句
- 不要改写任何原文、不要解释、不要输出别的字段"""

SCHEMA = {"type": "object",
          "properties": {"sections": {"type": "array", "items": {
              "type": "object",
              "properties": {"span": {"type": "array", "items": {"type": "integer"}},
                             "breaks": {"type": "array", "items": {"type": "integer"}}},
              "required": ["span", "breaks"]}}},
          "required": ["sections"]}

CJK = re.compile(r"[\u4e00-\u9fff]")


def sentences(text):
    """与 Java 侧同一套规则：句末标点切；换行两侧有一侧不含中文才切"""
    out, start, line_start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c in "。！？!?；;":
            out.append(text[start:i + 1]); start = i + 1; line_start = i + 1
        elif c == "\n":
            j = text.find("\n", i + 1)
            j = len(text) if j < 0 else j
            if not CJK.search(text[line_start:i]) or not CJK.search(text[i + 1:j]):
                out.append(text[start:i + 1]); start = i + 1
            line_start = i + 1
        i += 1
    if start < len(text):
        out.append(text[start:])
    return [s for s in out if s.strip()] or [text]


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ask(sents):
    numbered = "\n".join(f"{i+1}. {s.strip()}" for i, s in enumerate(sents))
    t0 = time.time()
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                           "options": {"temperature": 0.1, "num_ctx": 24576},
                           "messages": [{"role": "system",
                                         "content": PROMPT.replace("N", str(len(sents)))},
                                        {"role": "user", "content": numbered}]})
    ms = int((time.time() - t0) * 1000)
    txt = r.get("message", {}).get("content", "")
    try:
        return json.loads(txt).get("sections", []), ms
    except Exception:
        m = re.search(r'"sections"\s*:\s*\[', txt)
        return ([], ms) if not m else ([], ms)


def verify(sections, n):
    """机械校验三件事：章首尾相接覆盖 1..N、breaks 在章内递增、章数≥1"""
    if not sections:
        return False, "没解析出 sections"
    pos = 1
    for s in sections:
        span = s.get("span") or []
        if len(span) != 2:
            return False, "span 形状不对"
        a, b = span
        if a != pos:
            return False, f"章不连续：期望从 {pos} 开始，实得 {a}"
        if b < a or b > n:
            return False, f"章范围越界：{a}..{b}（N={n}）"
        prev = a - 1
        for br in (s.get("breaks") or []):
            if not (a <= br <= b) or br <= prev:
                return False, f"断点非法：{br}（章 {a}..{b}）"
            prev = br
        pos = b + 1
    if pos != n + 1:
        return False, f"末章没到 N：覆盖到 {pos-1}/{n}"
    return True, "ok"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    files = sorted(f for f in os.listdir(STORAGE) if f.endswith(".md"))[:want]
    print(f"取 {len(files)} 篇整篇文档\n")
    print(f"{'文档':<30}{'句数':>5}{'章':>4}{'节':>4}{'合法':>6}{'耗时':>8}   备注")

    records, ok_cnt = [], 0
    for f in files:
        text = io.open(os.path.join(STORAGE, f), encoding="utf-8", errors="replace").read()
        ss = sentences(text)
        secs, ms = ask(ss)
        good, why = verify(secs, len(ss))
        ok_cnt += good
        n_sec = len(secs)
        n_sub = sum(len(s.get("breaks") or []) + 1 for s in secs) if secs else 0
        print(f"{f[:28]:<30}{len(ss):>5}{n_sec:>4}{n_sub:>4}"
              f"{('✅' if good else '❌'):>6}{ms:>7}ms   {why if not good else ''}")
        records.append({"file": f, "n_sent": len(ss), "sections": secs,
                        "ok": good, "why": why, "ms": ms, "sents": ss})

    print(f"\n合法 {ok_cnt}/{len(files)}")
    # 层级尺寸：章覆盖多少句、节覆盖多少句
    for r in records:
        if not r["ok"]:
            continue
        sizes, sub = [], []
        for s in r["sections"]:
            a, b = s["span"]
            sizes.append(b - a + 1)
            prev = a - 1
            for br in (s["breaks"] or []) + [b]:
                sub.append(br - prev)
                prev = br
        print(f"  {r['file'][:26]:<28} 章 {len(sizes)} 个（句数 {min(sizes)}~{max(sizes)}）　"
              f"节 {len(sub)} 个（句数 {min(sub)}~{max(sub)}）")
    json.dump([{k: v for k, v in r.items() if k != "sents"} for r in records],
              io.open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
