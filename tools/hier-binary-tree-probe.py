# -*- coding: utf-8 -*-
"""
用二值判断递归建树：每一步只问「转换点在前半还是后半」，输入给**真实正文摘录**。

与上一次（标签序列 + 一次定章）的两处不同，都是针对那次失败改的：
  · 输出退化成「全倒进最后一章」→ 这里每步只回答二选一，退化了也能机械识别
  · 只给标签 → 标签里没有「这里开始是代码了」这个信息，而正文里有

判据也换成可机械计算的：**边界落在哪里才算好**。做法是给每段打一个「代码味」标记
（非中文字符占比高、或匹配代码特征），若边界两侧都是代码味，就判定它切在代码中间 ——
上一次肉眼看出 6 个边界里 2~3 个是这样，这次让它自己数。

用法：python tools/hier-binary-tree-probe.py [文档id] [最小叶子段数]
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
CACHE = os.path.join(HERE, "..", "data", "_bintree-segs.json")
CHAT = "qwen3:4b"
BS = chr(92)

LEAF_PROMPT = """下面这段资料已按句子编号（1 到 N）。请按**语义**把它切成若干段，每段讲一件事。

只输出每段的**结束句编号**，JSON：{"ends":[3,7,12]}

要求：
- 编号从 1 开始、严格递增，**最后一项必须等于 N**
- 每段 2~8 句；不要把整段当成一段，也不要逐句切
- 不要改写、不要解释"""

# 二值决策：只回答「前半还是后半」。示例值同样不能写成会被抄的实词。
SPLIT_PROMPT = """下面是同一篇文档中一段连续区间的内容摘录，分「前半」和「后半」两组。

请判断：这段区间里**最大的一次主题转换**靠近哪一半？

只输出 JSON：{"half":"A"}

要求：
- "A" 表示转换点在前半，"B" 表示在后半
- 判断依据是「从这里开始讲的东西明显换了一件」，不是文字风格变化
- 两组看起来仍在讲同一件事时，选**内容变化更明显**的那一半
- 不要解释"""

SPLIT_SCHEMA = {"type": "object", "properties": {"half": {"type": "string"}}, "required": ["half"]}
LEAF_SCHEMA = {"type": "object",
               "properties": {"ends": {"type": "array", "items": {"type": "integer"}}},
               "required": ["ends"]}
CJK = re.compile(r"[\u4e00-\u9fff]")
CODEY = re.compile(r"[{}();=<>\[\]]|^\s*(public|private|import|class|def|SELECT|while|for|if)\b",
                   re.MULTILINE)


def psql_rows(sql):
    f = os.path.join(HERE, "_bt.sql")
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


def psql(sql):
    f = os.path.join(HERE, "_bt2.sql")
    io.open(f, "w", encoding="utf-8").write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    return subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                           "-t", "-A", "-f", f], capture_output=True, env=env).stdout.decode("utf-8", "replace")


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


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
    return [s for s in out if s.strip()] or [text]


def ask(system, user, schema):
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": schema,
                           "options": {"temperature": 0.1, "num_ctx": 16384},
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]})
    try:
        return json.loads(r.get("message", {}).get("content", ""))
    except Exception:
        return {}


def codey(seg):
    """代码味：非中文占多数，或命中代码特征"""
    cjk = len(CJK.findall(seg))
    return (cjk / max(len(seg), 1) < 0.35) or bool(CODEY.search(seg))


def excerpts(segs, lo, mid, hi, n=2):
    a = segs[lo:min(lo + n, mid)] + segs[max(lo, mid - n):mid]
    b = segs[mid:min(mid + n, hi)] + segs[max(mid, hi - n):hi]
    fmt = lambda xs: "\n".join(f"- {x[:110].replace(chr(10), ' ')}" for x in xs)
    return f"【前半】\n{fmt(a)}\n\n【后半】\n{fmt(b)}"


def find_split(segs, lo, hi, calls, repairs, answers, min_gap=4):
    """
    二值搜索找出 [lo,hi) 内最大的一次转换点：每步只问「前半还是后半」，
    收窄直到窗口小到 min_gap 以内，返回该位置。
    """
    a, b = lo, hi
    while b - a > min_gap:
        mid = (a + b) // 2
        d = ask(SPLIT_PROMPT, excerpts(segs, a, mid, b), SPLIT_SCHEMA)
        half = str(d.get("half", "")).strip().upper()[:1]
        calls[0] += 1
        answers.append(half)
        if half == "A":
            b = mid
        elif half == "B":
            a = mid
        else:
            repairs[0] += 1
            break                      # 无效就停在当前中点，不硬猜
    return (a + b) // 2


def build(segs, lo, hi, min_leaf, calls, repairs, answers, out):
    """递归建树：先在窗口里找一个转换点，再对两半各自递归"""
    if hi - lo <= min_leaf:
        out.append((lo, hi))
        return
    at = find_split(segs, lo, hi, calls, repairs, answers)
    if at <= lo or at >= hi:           # 保险：切点必须在窗口内部
        at = (lo + hi) // 2
    build(segs, lo, at, min_leaf, calls, repairs, answers, out)
    build(segs, at, hi, min_leaf, calls, repairs, answers, out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    doc = sys.argv[1] if len(sys.argv) > 1 else None
    min_leaf = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    if not doc:
        doc = psql("SELECT doc_id FROM chunks GROUP BY doc_id ORDER BY count(*) DESC LIMIT 1;").strip().splitlines()[0].strip()
    name = psql(f"SELECT name FROM documents WHERE id='{doc}';").strip().splitlines()[0]

    if os.path.exists(CACHE) and json.load(io.open(CACHE, encoding="utf-8")).get("doc") == doc:
        segs = json.load(io.open(CACHE, encoding="utf-8"))["segs"]
        print(f"文档：{name[:44]}　复用缓存 {len(segs)} 段\n")
    else:
        chunks = psql_rows(f"SELECT content FROM chunks WHERE doc_id='{doc}' ORDER BY seq")
        print(f"文档：{name[:44]}　{len(chunks)} 块　（叶子层：模型逐块切分）")
        segs, t0 = [], time.time()
        for c in chunks:
            ss = sentences(c)
            if len(ss) < 3:
                segs.append(c)
                continue
            d = ask(LEAF_PROMPT.replace("N", str(len(ss))),
                    "\n".join(f"{j+1}. {s.strip()}" for j, s in enumerate(ss)), LEAF_SCHEMA)
            e = sorted({x for x in d.get("ends", []) if isinstance(x, int) and 1 <= x <= len(ss)}) or [len(ss)]
            if e[-1] != len(ss):
                e.append(len(ss))
            st = 0
            for x in e:
                piece = "".join(ss[st:x]).strip()
                if piece:
                    segs.append(piece)
                st = x
        print(f"  {len(chunks)} 块 → {len(segs)} 段，{time.time()-t0:.0f}s\n")
        json.dump({"doc": doc, "segs": segs}, io.open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

    print(f"—— 二值递归建树（最小叶子 {min_leaf} 段）——")
    calls, repairs, answers, out = [0], [0], [], []
    t0 = time.time()
    build(segs, 0, len(segs), min_leaf, calls, repairs, answers, out)
    na, nb = answers.count("A"), answers.count("B")
    print(f"  调用 {calls[0]} 次，机械修复 {repairs[0]} 次，耗时 {time.time()-t0:.0f}s")
    print(f"  模型的回答分布：A（前半）{na} 次，B（后半）{nb} 次"
          f"　{'⚠️ 全偏向一侧，等于没有判断' if min(na, nb) == 0 and answers else ''}")
    print(f"  叶子 {len(out)} 个，段数 {[b-a for a, b in out]}")

    # 边界质量：边界两侧是不是都「代码味」（那说明切在代码中间）
    bad = []
    for i in range(1, len(out)):
        j = out[i][0]
        if j <= 0 or j >= len(segs):
            continue
        if codey(segs[j - 1]) and codey(segs[j]):
            bad.append(j)
    print(f"\n  边界 {len(out)-1} 个，其中两侧都是代码味的 **{len(bad)} 个**"
          f"（=切在代码/日志中间，不是主题转换）")
    for j in bad:
        print(f"    ✗ 段{j}→{j+1}: …{segs[j-1][-40:].strip()}  ||  {segs[j][:40].strip()}…")
    print("\n  边界两侧原文（前 3 个）：")
    for i in range(1, min(4, len(out))):
        j = out[i][0]
        print(f"    {i}|{i+1}  …{segs[j-1][-44:].strip()}")
        print(f"          {segs[j][:44].strip()}…")


if __name__ == "__main__":
    main()
