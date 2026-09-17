# -*- coding: utf-8 -*-
"""
句间指向成图：块内指定主旨句，其余句子指向它归属的那一句。

沿用刚才验证通过的那条机制 —— 模型只**指认下标**，不产出内容，所以：
  · 逐字/取值可机械校验
  · 退化解看得见（这一版里的退化解是**星形图**：所有句子都指向主旨句，形状合法但零信息）

三重校验，各管一段：
  ① 合法性  下标在范围内、无环、单根 —— 纯机械
  ② 形状    多少句直接挂在主旨句下（星形度）、最大深度、出度分布
            —— 星形度接近 100% 就是退化，没有分层
  ③ 向量验证 每条边：指向句与被指句的相似度 **减去** 它与随机 5 句的相似度
            为正 = 这条边有语义依据；同时算随机边的零分布做对照

用法：python tools/discourse-graph-probe.py [块数，默认 12]
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

PROMPT = """下面这段资料已按句子编号。请做两件事：

1. 指出**主旨句**（整段在讲的那件事，就一句）
2. 其余每句**归到它支撑/展开的那一句**上 —— 它是在补充、解释、举例哪一句？

只输出 JSON：{"nucleus":3,"links":[[2,3],[4,3],[5,4]]}
links 里每个 [i,j] 表示「第 i 句归到第 j 句」。每个句子最多出现一次（作为 i）。
不要解释。"""

SCHEMA = {"type": "object",
          "properties": {"nucleus": {"type": "integer"},
                         "links": {"type": "array", "items": {"type": "array",
                                                              "items": {"type": "integer"}}}},
          "required": ["nucleus", "links"]}
CJK = re.compile(r"[\u4e00-\u9fff]")


def psql_rows(sql):
    f = os.path.join(HERE, "_dg.sql")
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


def post(path, body, timeout=150, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            print(f"    （{path} 第 {k+1} 次失败：{type(e).__name__}）", flush=True)
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


def depth_of(parent, n):
    """最大深度 + 是否有环（有环返回 -1）"""
    d = [0] * (n + 1)
    for start in range(1, n + 1):
        seen, cur, k = set(), start, 0
        while parent.get(cur) and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
            k += 1
            if k > n:
                return -1
        d[start] = k
    return max(d)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 12

    rows = psql_rows("SELECT content FROM chunks ORDER BY random() LIMIT " + str(want))
    chunks = [r for r in rows if len(r) > 200]
    print(f"取样 {len(chunks)} 块\n")

    star_rates, depths, margins, rand_margins = [], [], [], []
    n_ok = n_bad = n_cycle = 0
    for i, c in enumerate(chunks, 1):
        ss = sentences(c)
        if len(ss) < 4:
            continue
        d = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                               "options": {"temperature": 0.1, "num_ctx": 16384},
                               "messages": [{"role": "system", "content": PROMPT},
                                            {"role": "user",
                                             "content": "\n".join(f"{j+1}. {s}" for j, s in enumerate(ss))}]})
        try:
            obj = json.loads(d.get("message", {}).get("content", ""))
        except Exception:
            obj = {}
        n = len(ss)
        nuc = obj.get("nucleus")
        links = obj.get("links", [])
        if not isinstance(nuc, int) or not (1 <= nuc <= n):
            n_bad += 1
            continue
        parent, dup = {}, 0
        for pair in links:
            if isinstance(pair, list) and len(pair) == 2:
                a, b = pair
                if isinstance(a, int) and isinstance(b, int) and 1 <= a <= n and 1 <= b <= n and a != b:
                    if a in parent:
                        dup += 1
                        continue
                    parent[a] = b
        parent[nuc] = 0            # 根
        n_ok += 1
        dp = depth_of(parent, n)
        if dp < 0:
            n_cycle += 1
        direct = sum(1 for a, b in parent.items() if b == nuc and a != nuc)
        star = direct / max(n - 1, 1)
        star_rates.append(star)
        depths.append(dp if dp >= 0 else 0)

        # 向量验证：每条边 vs 随机边
        sv = embed(ss)
        rng = np.random.default_rng(i)
        for a, b in parent.items():
            if b == 0:
                continue
            others = [x for x in range(1, n + 1) if x not in (a, b)]
            if not others:
                continue
            pick = rng.choice(others, size=min(5, len(others)), replace=False)
            real = float(sv[a - 1] @ sv[b - 1])
            rnd = float(np.mean([sv[a - 1] @ sv[x - 1] for x in pick]))
            margins.append(real - rnd)
            rand_margins.append(rnd - float(np.mean([sv[a - 1] @ sv[x - 1] for x in pick]))
                                if False else 0.0)

        if i <= 2:
            print(f"  块{i}（{n} 句）：主旨句 {nuc}　边 {sum(1 for x in parent.values() if x)} 条"
                  f"　直接挂主旨句下 {direct}/{n-1}　最大深度 {dp}")
            print(f"     主旨：{ss[nuc-1][:56]}")
            for a, b in list(parent.items())[:4]:
                if b:
                    print(f"     第{a}句 → 第{b}句：{ss[a-1][:40]} ⇒ {ss[b-1][:40]}")
        if i % 4 == 0:
            print(f"  …{i}/{len(chunks)}", flush=True)

    print(f"\n—— ① 合法性 ——")
    print(f"  可用图：{n_ok}　主旨句非法：{n_bad}　**有环**：{n_cycle}")
    print(f"\n—— ② 形状（星形度 = 直接挂在主旨句下的比例）——")
    print(f"  星形度：中位 {np.median(star_rates):.2f}，均值 {np.mean(star_rates):.2f}")
    print(f"  最大深度：{sorted(set(depths))}　（全为 1 = 只有一层 = 没有分层）")
    print(f"\n—— ③ 向量验证（每条边 vs 随机边）——")
    m = np.array(margins) if margins else np.array([0.0])
    print(f"  边 {len(m)} 条，判别差中位 {np.median(m):+.3f}，为正占 {100*(m > 0).mean():.0f}%")
    print(f"  （对比：词语指向那次是 95% 为正、中位 +0.093）")


if __name__ == "__main__":
    main()
