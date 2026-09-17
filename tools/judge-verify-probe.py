# -*- coding: utf-8 -*-
"""
独立裁判验证：换一个**别的家族**的模型来判「这句在说明哪一句」，盲评对照。

为什么要换裁判：余弦相似度那把尺子天然偏向局部连贯，于是「指前一句」这个懒答案
总能赢。而话语关系（说明/展开）经常是**换词**的，相似度看不见。

裁判用 llama3.1:8b（Meta，与造数据的 qwen3:4b 不同族）；本机还有 qwen3:8b / qwen3.5:9b，
但同族判断相关性太高，不取。

两个对照，缺一不可：
  主测  模型选的那句 vs 「前一句」基线   —— 打赢才说明模型真在判断
  对照  模型选的那句 vs 随机一句         —— 这个也打不赢，说明模型的选择是噪声
  （顺序随机，避免位置偏置；裁判不知道哪个是模型选的）

用法：python tools/judge-verify-probe.py [块数，默认 4]
"""
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
OTHERS = os.path.join(HERE, "..", "data", "_judge-control.json")
MAKER, JUDGE = "qwen3:4b", "llama3.1:8b"
BS = chr(92)

CHOICE_PROMPT = """下面是资料的前 {K} 句（已编号）。请判断：**第 {K} 句**在说明前面的哪一句？

只输出 JSON：{{"at":2}}

要求：
- at 是 1 到 {K1} 之间的某个句号（只能指前文）
- 选**它真正在补充、解释或举例的那一句**；仅仅紧挨着不算理由
- 不要解释"""

CHOICE_SCHEMA = {"type": "object", "properties": {"at": {"type": "integer"}}, "required": ["at"]}

JUDGE_PROMPT = """有人把一句话归到另一句话名下。请判断这个归属对不对。

【被归属的句子】（下称「它」）
{i}

【候选 A】
{a}

【候选 B】
{b}

问题：「它」更是在说明/展开 A 还是 B？

只输出 JSON：{"pick":"A"}"""
JUDGE_SCHEMA = {"type": "object", "properties": {"pick": {"type": "string"}}, "required": ["pick"]}
CJK = re.compile(r"[\u4e00-\u9fff]")


def psql_rows(sql):
    f = os.path.join(HERE, "_jv.sql")
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


def post(model, system, user, schema, num_ctx, timeout=150, tries=3):
    body = {"model": model, "stream": False, "think": False, "format": schema,
            "options": {"temperature": 0.1, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(json.load(r).get("message", {}).get("content", ""))
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
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    rng = random.Random(20260917)

    # ---------- 第一阶段：用 qwen3:4b 收集边 ----------
    rows = psql_rows("SELECT content FROM chunks ORDER BY random() LIMIT " + str(want))
    chunks = [r for r in rows if 300 < len(r) < 2000]
    print(f"取样 {len(chunks)} 块　制造者 {MAKER}\n—— ① 收集边（一句一问）——", flush=True)
    cases = []
    for ci, c in enumerate(chunks, 1):
        ss = sentences(c)
        if len(ss) < 5:
            continue
        for k in range(2, len(ss) + 1):
            body = CHOICE_PROMPT.replace("{K}", str(k)).replace("{K1}", str(k - 1))
            d = post(MAKER, body, "\n".join(f"{j+1}. {s}" for j, s in enumerate(ss[:k])),
                     CHOICE_SCHEMA, 16384)
            at = d.get("at")
            if isinstance(at, int) and 1 <= at <= k - 1:
                cases.append({"chunk": ci, "i": k, "sents": ss, "model": at, "base": k - 1})
        print(f"   块{ci}　累计 {len(cases)} 条边", flush=True)

    # ---------- 第二阶段：换裁判盲评 ----------
    print(f"\n—— ② 盲评（裁判 {JUDGE}，与制造者不同族）——", flush=True)
    win_base = win_rand = n = 0
    for idx, cs in enumerate(cases, 1):
        ss, i, mj = cs["sents"], cs["i"], cs["model"]
        pool = [x for x in range(1, i) if x not in (mj, cs["base"])]
        rj = rng.choice(pool) if pool else None
        # 主测：模型选的那句 vs 「前一句」基线
        for label, other, is_base in (("base", cs["base"], True),
                                      ("rand", rj, False)):
            if other is None or other == mj:
                continue
            flip = rng.random() < 0.5
            a, b = (other, mj) if flip else (mj, other)
            d = post(JUDGE, JUDGE_PROMPT.replace("{i_short}", "这句话"),
                     JUDGE_PROMPT.split("\n\n只输出")[0].format(
                         i=ss[i - 1], a=ss[a - 1], b=ss[b - 1]),
                     JUDGE_SCHEMA, 8192)
            pick = str(d.get("pick", "")).strip().upper()[:1]
            picked_model = (pick == ("B" if flip else "A"))
            if is_base:
                win_base += int(picked_model)
            else:
                win_rand += int(picked_model)
            n += 1
        if idx % 10 == 0:
            print(f"   边 {idx}/{len(cases)}　vs基线 {win_base}　vs随机 {win_rand}", flush=True)

    nb = len(cases)
    print(f"\n—— 结果（裁判 {JUDGE}，盲评）——")
    print(f"  模型选的那句 **赢过「前一句」基线**：{win_base}/{nb} = {100*win_base/max(nb,1):.0f}%"
          f"　（≈50% = 无差别；>65% 才算有信号）")
    print(f"  模型选的那句 **赢过随机一句**：{win_rand}/{nb} = {100*win_rand/max(nb,1):.0f}%"
          f"　（这个也≈50% 说明模型的选择是噪声）")


if __name__ == "__main__":
    main()
