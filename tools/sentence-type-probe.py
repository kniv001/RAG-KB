# -*- coding: utf-8 -*-
"""
句子类型打标：模型判 vs 规则判，逐标签比对。

为什么这一类任务和「话语关系」不同（那个刚被判否）：
  · 话语关系的正确答案只在语义里 → 只能靠语义判 → 两把尺子都判否
  · **句子类型的答案在字面上** → 标点/虚词/句式都能查 → 有机械判据

所以它是「指向」机制能成立的那一类。判据（中文标记词，可靠性高）：
  疑问   ？/吗/呢/如何/怎么/是否/多少
  条件   如果/若/一旦/假如/当…时/除非
  因果   因为/所以/由于/因此/导致/使得/因而
  转折   但是/然而/不过/却/反之/otherwise
  举例   例如/比如/举例/示例/如下

做法：一句话一次调用，输出适用标签的集合（封闭 5 元）；再逐标签与规则对照。
两个退化口都看得见：全判「都没有」→ 召回塌；全判「全都有」→ 精确塌。

用法：python tools/sentence-type-probe.py [句数，默认 60]
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT = "qwen3:4b"
BS = chr(92)

LABELS = ["疑问", "条件", "因果", "转折", "举例"]
RULES = {
    "疑问": r"[？?]|吗[，。！？\s]|呢[，。！？\s]|如何|怎么|是否|多少|为什么|哪些",
    "条件": r"如果|若[^干]|一旦|假如|除非|当.{0,12}时[，,]|在.{0,10}情况下",
    "因果": r"因为|所以|由于|因此|导致|使得|因而|从而",
    "转折": r"但是|然而|不过|却[^是]|反之|与此相反",
    "举例": r"例如|比如|举例|示例|如下|譬如|比如说",
}

# 输出改成**五个必填布尔字段** —— 不给自己留「空」这个出口。
# 上一版写了「也可以一个都不选」，模型 60 句里 54 句判空，召回塌到 0~22%。
# 这与命题抽取那次同因：任何「允许不做」的口子都会被照单全收。
PROMPT = """对这句话逐类判断，五类都要给出答案（是/否）：

- 疑问：在提问（问号、吗、呢、如何、是否）
- 条件：含条件从句（如果……则、一旦、当……时）
- 因果：讲因果（因为、所以、由于、因此、导致）
- 转折：含转折（但是、然而、不过、却）
- 举例：在举例（例如、比如、如下）

只输出 JSON：{"疑问":false,"条件":false,"因果":false,"转折":false,"举例":false}"""

SCHEMA = {"type": "object",
          "properties": {k: {"type": "boolean"} for k in ["疑问", "条件", "因果", "转折", "举例"]},
          "required": ["疑问", "条件", "因果", "转折", "举例"]}
CJK = re.compile(r"[\u4e00-\u9fff]")


def psql_rows(sql):
    f = os.path.join(HERE, "_st.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        for esc, real in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(esc, real)
        out.append(line)
    return out


def post(body, timeout=120, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(2)
    return {}


def sentences(text):
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
    return [s.strip() for s in out if s.strip()] or [text]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 60

    rows = psql_rows("SELECT content FROM chunks ORDER BY random() LIMIT 12")
    pool = []
    for r in rows:
        for s in sentences(r):
            if 12 <= len(s) <= 160 and CJK.search(s):
                pool.append(s)
    import random
    random.Random(20260917).shuffle(pool)
    sents = pool[:want]
    print(f"取 {len(sents)} 句（5 个标签：{'/'.join(LABELS)}）\n")

    stat = {L: {"tp": 0, "fp": 0, "fn": 0} for L in LABELS}
    exact = empty_model = empty_rule = 0
    n = 0
    samples = []
    t0 = time.time()
    for i, s in enumerate(sents, 1):
        rule = {L for L, pat in RULES.items() if re.search(pat, s)}
        d = post({"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                  "options": {"temperature": 0.1, "num_ctx": 16384},
                  "messages": [{"role": "system", "content": PROMPT},
                               {"role": "user", "content": s}]})
        try:
            got = {t for t in json.loads(d.get("message", {}).get("content", "")).get("types", [])
                   if t in LABELS}
        except Exception:
            got = set()
        n += 1
        if got == rule:
            exact += 1
        if not got:
            empty_model += 1
        if not rule:
            empty_rule += 1
        for L in LABELS:
            if L in got and L in rule:
                stat[L]["tp"] += 1
            elif L in got:
                stat[L]["fp"] += 1
            elif L in rule:
                stat[L]["fn"] += 1
        if got != rule and len(samples) < 6:
            samples.append((s, rule, got))
        if i % 20 == 0:
            print(f"  {i}/{len(sents)}　完全一致 {exact}/{n}", flush=True)

    print(f"\n耗时 {time.time()-t0:.0f}s")
    print(f"\n—— 逐标签（模型 vs 规则）——")
    print(f"{'标签':>6}{'命中':>6}{'误报':>6}{'漏报':>6}{'精确':>8}{'召回':>8}")
    for L in LABELS:
        s_ = stat[L]
        p = s_["tp"] / max(s_["tp"] + s_["fp"], 1)
        r = s_["tp"] / max(s_["tp"] + s_["fn"], 1)
        print(f"{L:>6}{s_['tp']:>6}{s_['fp']:>6}{s_['fn']:>6}{100*p:>7.0f}%{100*r:>7.0f}%")
    print(f"\n—— 整体 ——")
    print(f"  标签集合完全一致：{exact}/{n} = {100*exact/max(n,1):.0f}%")
    print(f"  模型判「都没有」：{empty_model}/{n}　规则判「都没有」：{empty_rule}/{n}"
          f"　（两个退化口：全判空 → 召回塌；全判满 → 精确塌）")
    print(f"\n—— 分歧样例（看谁错）——")
    for s, rule, got in samples:
        print(f"  句：{s[:52]}")
        print(f"    规则 {sorted(rule)}　模型 {sorted(got)}")


if __name__ == "__main__":
    main()
