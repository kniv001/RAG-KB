# -*- coding: utf-8 -*-
"""
批量塌陷：为什么单发正确、批量跑全错。

现象（今天撞了两次）：
  · 抽取命题那次：74 段里 53 段返回空数组，而重跑只有 4~5 段空 —— 不可复现
  · 句子类型这次：批量 60 句全判「五类都是 false」，而手工单发三句全对

怀疑：**连续快速发「系统提示词逐字相同」的请求，前缀 KV 被复用，模型塌到退化解**。
这与项目里那条「前缀复用省 95% prefill」是同一机制的阴暗面 —— 省了 prefill，也可能
把不该复用的状态复用了。

对照三组，每句发一次：
  A 原样（系统提示词逐字相同）
  B 系统提示词里塞一个唯一 nonce（打破前缀缓存）
  C 原样但在两次调用之间停 1.5 秒

判据：空判率（五个 false 的比例）。手工单发的基线是 0%。

用法：python tools/batch-collapse-probe.py [每组句数，默认 20]
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

PROMPT = """对这句话逐类判断，五类都要给出答案（是/否）：

- 疑问：在提问（问号、吗、呢、如何、是否）
- 条件：含条件从句（如果……则、一旦、当……时）
- 因果：讲因果（因为、所以、由于、因此、导致）
- 转折：含转折（但是、然而、不过、却）
- 举例：在举例（例如、比如、如下）

只输出 JSON：{"疑问":false,"条件":false,"因果":false,"转折":false,"举例":false}"""

SCHEMA = {"type": "object",
          "properties": {k: {"type": "boolean"} for k in LABELS},
          "required": LABELS}
RULES = {
    "疑问": r"[？?]|吗[，。！？\s]|呢[，。！？\s]|如何|怎么|是否|多少|为什么|哪些",
    "条件": r"如果|若[^干]|一旦|假如|除非|当.{0,12}时[，,]|在.{0,10}情况下",
    "因果": r"因为|所以|由于|因此|导致|使得|因而|从而",
    "转折": r"但是|然而|不过|却[^是]|反之|与此相反",
    "举例": r"例如|比如|举例|示例|如下|譬如|比如说",
}


def psql_rows(sql):
    f = os.path.join(HERE, "_bc.sql")
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


def ask(system, user, timeout=120):
    body = {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.1, "num_ctx": 16384},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = json.load(r).get("message", {}).get("content", "")
    try:
        o = json.loads(txt)
        return {k for k in LABELS if o.get(k) is True}, len(txt)
    except Exception:
        return None, len(txt)


CJK = re.compile(r"[\u4e00-\u9fff]")


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


def run(name, sents, mode):
    empt = parse_fail = 0
    hits = 0
    t0 = time.time()
    for idx, s in enumerate(sents, 1):
        sysp = PROMPT if mode != "nonce" else PROMPT + f"\n\n（编号 {idx}-{int(time.time()*1000)%100000}）"
        got, _ = ask(sysp, s)
        rule = {L for L, p in RULES.items() if re.search(p, s)}
        if got is None:
            parse_fail += 1
        else:
            if not got:
                empt += 1
            hits += len(got & rule)
        if mode == "sleep":
            time.sleep(1.5)
    n = len(sents)
    print(f"  {name:<26} 空判 {empt}/{n} = {100*empt/max(n,1):>3.0f}%　"
          f"解析失败 {parse_fail}　命中标签 {hits}　耗时 {time.time()-t0:.0f}s", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    rows = psql_rows("SELECT content FROM chunks ORDER BY random() LIMIT 10")
    pool = []
    for r in rows:
        for s in sentences(r):
            if 12 <= len(s) <= 160 and CJK.search(s):
                pool.append(s)
    import random
    random.Random(7).shuffle(pool)
    sents = pool[:want]
    print(f"每组 {len(sents)} 句（同批句子，三种发法）\n")
    run("A 原样（前缀逐字相同）", sents, "plain")
    run("B 系统提示词加唯一 nonce", sents, "nonce")
    run("C 原样 + 每次停 1.5 秒", sents, "sleep")


if __name__ == "__main__":
    main()
