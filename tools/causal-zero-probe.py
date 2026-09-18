# -*- coding: utf-8 -*-
"""
「因果」召回恒 0：是模型处理不了因果句，还是别的原因？

背景：sentence-type-probe 两版里，正则判「因果」为真的句子模型一次都没判出来（16/16 全漏）。
用户问：是模型完全处理不了这类句子吗？

「能力不足」的预测分布是「难句错、带字面标记的易句对」。实测恰好相反（标记句也漏），
所以本实验直接验：把**带 因为/所以/由于/因此/导致 的语料句**逐条单发，打印完整五元输出。

三种结局指向三个不同结论：
  甲 语料句五元全 false、哨兵正常   → 这些句子触发塌陷，与「因果」标签无关
  乙 判了别的标签、唯独因果 false   → 模型把因果句读成别的类（口径/提示词问题，可救）
  丙 连哨兵里的因果句也判 false     → 才轮到「这个标签它做不到」

哨兵（自造、答案已知）首尾各发一遍：哨兵错 ⇒ 环境/提示词有问题，语料结果不读；
首尾不一致 ⇒ 越跑越塌（漂移），只读前半。

用法：python tools/causal-zero-probe.py [语料句数，默认 20]
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

# 与 sentence-type-probe.py **逐字相同**，否则结果不可比
RULES = {
    "疑问": r"[？?]|吗[，。！？\s]|呢[，。！？\s]|如何|怎么|是否|多少|为什么|哪些",
    "条件": r"如果|若[^干]|一旦|假如|除非|当.{0,12}时[，,]|在.{0,10}情况下",
    "因果": r"因为|所以|由于|因此|导致|使得|因而|从而",
    "转折": r"但是|然而|不过|却[^是]|反之|与此相反",
    "举例": r"例如|比如|举例|示例|如下|譬如|比如说",
}
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

# 哨兵：答案由我给定。前三个查「因果能不能判出来」，其余每类各一，最后一个是反口
# （五类都不是），查「会不会全判满」。
SENTINELS = [
    ("因为缓存失效，所以请求全部落到了数据库上。", {"因果"}),
    ("这个异常是由于没有释放连接导致的。", {"因果"}),
    ("服务在高峰期崩溃过三次，根因是连接池没有回收。", {"因果"}),  # 无标记的语义因果
    ("如果连接超时，就重试三次。", {"条件"}),
    ("但是这个方案的缺陷在于扩展性。", {"转折"}),
    ("例如，可以用哈希表把查找降到常数时间。", {"举例"}),
    ("内存为什么会一直涨？", {"疑问"}),
    ("这个函数返回一个整数。", set()),
]
CAUSAL = re.compile(RULES["因果"])
CJK = re.compile(r"[\u4e00-\u9fff]")
THINK = False


def psql_rows(sql):
    f = os.path.join(HERE, "_cz.sql")
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


def ask(text, timeout=300, tries=3):
    body = {"model": CHAT, "stream": False, "think": THINK, "format": SCHEMA,
            "options": {"temperature": 0.1, "num_ctx": 16384},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": text}]}
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            m = d.get("message", {})
            raw = m.get("content", "")
            th = len(m.get("thinking") or "")
            try:
                o = json.loads(raw)
                if isinstance(o, dict) and all(k in o for k in LABELS):
                    return {k for k in LABELS if o[k] is True}, raw, th
                return None, raw, th      # 残破 JSON → 原样打印
            except Exception:
                return None, raw, th
        except Exception as e:
            last = e
            time.sleep(2)
    return None, f"<HTTP 失败 {last}>", 0


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


def one(tag, text, expect=None):
    got, raw, th = ask(text)
    if got is None:
        print(f"  [{tag}] {text[:46]}")
        print(f"        解析失败，原样：{raw[:120]}")
        return None
    mark = ""
    if expect is not None:
        mark = "  ✔" if got == expect else f"  ✘（哨兵期望 {sorted(expect) or '空'}）"
    tail = f"　思考 {th} 字" if THINK else ""
    print(f"  [{tag}] {text[:46]}")
    print(f"        规则 {sorted({L for L, p in RULES.items() if re.search(p, text)}) or '空'}"
          f"　模型 {sorted(got) or '全 false'}{mark}{tail}")
    return got


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    global THINK
    THINK = len(sys.argv) > 2 and sys.argv[2] == "think"
    if THINK:
        print("※ think:true 模式（与首轮 think:false 逐条对照；代价是每句多 1000~2000 字思考）\n")

    rows = psql_rows("SELECT content FROM chunks WHERE content ~ "
                     "'因为|所以|由于|因此|导致|使得|因而|从而' ORDER BY random() LIMIT 40")
    pool = []
    for r in rows:
        for s in sentences(r):
            if 12 <= len(s) <= 160 and CJK.search(s) and CAUSAL.search(s):
                pool.append(s)
    seen, uniq = set(), []
    for s in pool:
        if s not in seen:
            seen.add(s); uniq.append(s)
    if len(uniq) < 8:
        print(f"！目标集太小（{len(uniq)} 句），先别下结论 —— 放宽取块数重跑")
        return
    import random
    random.Random(20260917).shuffle(uniq)
    corpus = uniq[:want]
    print(f"语料池 {len(rows)} 块 → 正则判因果句 {len(uniq)} 条，取 {len(corpus)} 条")
    print(f"哨兵 {len(SENTINELS)} 条，首尾各发一遍\n")

    print("—— 哨兵（首）——")
    head_ok = 0
    for s, exp in SENTINELS:
        if one("哨兵", s, exp) == exp:
            head_ok += 1
    print(f"  首轮哨兵通过 {head_ok}/{len(SENTINELS)}"
          f"{'　⇒ 环境正常' if head_ok == len(SENTINELS) else '　⇒ 环境有问题，语料结果别读'}\n")

    print("—— 语料句（正则判因果为真）——")
    t0 = time.time()
    allfalse = causal_false = causal_true = parse_fail = 0
    dist = {L: 0 for L in LABELS}
    for s in corpus:
        got = one("语料", s)
        if got is None:
            parse_fail += 1
            continue
        if not got:
            allfalse += 1
        if "因果" in got:
            causal_true += 1
        else:
            causal_false += 1
        for L in got:
            dist[L] += 1
    n = max(causal_true + causal_false, 1)
    print(f"\n  耗时 {time.time()-t0:.0f}s　解析失败 {parse_fail}")
    print(f"  因果判真 {causal_true}/{n}　判 false {causal_false}/{n}"
          f"　整句五元全 false {allfalse}/{n}")
    print(f"  模型在这些句子上给出的标签分布：" +
          "　".join(f"{L} {dist[L]}" for L in LABELS))

    print("\n—— 哨兵（尾）——")
    tail_ok = 0
    for s, exp in SENTINELS:
        if one("哨兵", s, exp) == exp:
            tail_ok += 1
    print(f"  尾轮哨兵通过 {tail_ok}/{len(SENTINELS)}"
          f"{'　⇒ 无漂移' if tail_ok == len(SENTINELS) else '　⇒ 漂移，只读前半'}")


if __name__ == "__main__":
    main()
