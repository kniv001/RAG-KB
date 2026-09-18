# -*- coding: utf-8 -*-
"""
受限选择题：每一句回答「它在说明前文的哪一句」，选项 = 它前面的句子编号。

与上一支（句间成图）的三处不同，都是针对它的失败改的：
  · 一问一句 —— 堵掉「少指」（上一支六成句子根本没被连）
  · 选项限定前文 —— 图被约束成森林，无环无根外点（那两条检测因此无意义）
  · **自带零基线**：「每句指前一句」是这条路的机械退化版，逐句配对对比

关键判据（配对，不是看绝对相似度）：
  对同一句 i，模型选的那句 j 是不是比「前一句」更像它？
  若打不过，说明模型的选择等价于「指上句」这个懒答案，整条路没有增量。

顺带看两个形状量：选中的距离分布（都选 i-1 就是懒）、指向第 1 句的比例（星形度）。

用法：python tools/anaphora-choice-probe.py [块数，默认 6]
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT, EMBED = "qwen3:4b", "bge-m3"
BS = chr(92)

PROMPT = """下面是资料的前 {K} 句（已编号）。请判断：**第 {K} 句**在说明前面的哪一句？

只输出 JSON：{{"at":2}}

要求：
- at 是 1 到 {K1} 之间的某个句号（只能指前文）
- 选**它真正在补充、解释或举例的那一句**；仅仅紧挨着不算理由
- 不要解释"""

SCHEMA = {"type": "object", "properties": {"at": {"type": "integer"}}, "required": ["at"]}
CJK = re.compile(r"[\u4e00-\u9fff]")


def psql_rows(sql):
    f = os.path.join(HERE, "_an.sql")
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


def post(path, body, timeout=120, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


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


def embed(texts):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]})["embeddings"])
    a = np.array(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    rows = psql_rows("SELECT content FROM chunks ORDER BY md5(id::text) LIMIT " + str(want))
    chunks = [r for r in rows if 300 < len(r) < 2000]
    print(f"取样 {len(chunks)} 块，逐句一问\n")

    picks, dists, beat, margins, base_margins, illegal = [], [], 0, [], [], 0
    n_edges = 0
    for ci, c in enumerate(chunks, 1):
        ss = sentences(c)
        if len(ss) < 5:
            continue
        sv = embed(ss)
        rng = np.random.default_rng(ci)
        for k in range(2, len(ss) + 1):
            body = PROMPT.replace("{K}", str(k)).replace("{K1}", str(k - 1))
            try:
                d = post("/api/chat", {"model": CHAT, "stream": False,
                                       "think": os.environ.get("KB_THINK") == "1",
                                       "format": SCHEMA,
                                       "options": {"temperature": 0.1, "num_ctx": 16384},
                                       "messages": [{"role": "system", "content": body},
                                                    {"role": "user",
                                                     "content": "\n".join(f"{j+1}. {s}" for j, s in enumerate(ss[:k]))}]})
                at = json.loads(d.get("message", {}).get("content", "")).get("at")
            except Exception:
                at = None
            if not isinstance(at, int) or not (1 <= at <= k - 1):
                illegal += 1
                continue
            n_edges += 1
            picks.append(at)
            dists.append(k - at)
            # 配对对照：模型选的 vs 机械基线（前一句）
            others = [x for x in range(1, k) if x not in (at, k - 1)]
            pick = rng.choice(others, size=min(5, len(others)), replace=False) if others else [1]
            real = float(sv[k - 1] @ sv[at - 1])
            base = float(sv[k - 1] @ sv[k - 2])
            ref = float(np.mean([sv[k - 1] @ sv[x - 1] for x in pick]))
            margins.append(real - ref)
            base_margins.append(base - ref)
            if real > base + 1e-9:
                beat += 1
        print(f"  块{ci}/{len(chunks)}　累计边 {n_edges}　"
              f"打过基线 {beat}/{n_edges}", flush=True)

    print(f"\n—— ① 合法性 ——")
    print(f"  答题 {n_edges + illegal} 次，非法（越界/解析失败）{illegal}　"
          f"= {100*illegal/max(n_edges+illegal,1):.0f}%")
    print(f"\n—— ② 选择形状 ——")
    dd = np.array(dists)
    print(f"  指向前几句的距离：中位 {np.median(dd):.0f}，均值 {dd.mean():.1f}，最大 {dd.max()}")
    print(f"  选「紧挨着的前一句」的比例：{100*(dd == 1).mean():.0f}%"
          f"　（接近 100% = 等价于机械基线，等于没做判断）")
    print(f"  指向第 1 句的比例（星形度）：{100*np.mean([p == 1 for p in picks]):.0f}%")
    print(f"\n—— ③ 配对对照（关键）——")
    print(f"  模型选的比「前一句」更像的：**{beat}/{n_edges} = {100*beat/max(n_edges,1):.0f}%**"
          f"　（50% ≈ 与机械基线无差别）")
    m, b = np.array(margins), np.array(base_margins)
    print(f"  判别差中位：模型 {np.median(m):+.3f}　基线 {np.median(b):+.3f}")
    print(f"  （对照：词语指向那次 95% 为正、中位 +0.093）")


if __name__ == "__main__":
    main()
