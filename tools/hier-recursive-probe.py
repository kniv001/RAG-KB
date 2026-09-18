# -*- coding: utf-8 -*-
"""
两级结构，分两次做 —— 而不是让模型一次吞整篇。

上一支探针的结论：一次性对整篇文档做层级，4 篇全部返回「整篇一章、不再细分」，
每次只用 0.8~2.6 秒（222 句的文档不可能两秒读完）。与命题抽取那次同因 ——
**「合法的最短输出」就是逃生口**，而「不切」完美满足合法性校验。

改法：两层各在模型处理得动的规模上做。
  ① 叶子层：块内平面切分（已验证可行：263ms/块、零摆烂、逐字重构）
  ② 章层：把段当单位分组（段的编号 + 开头若干字，一次给几十条）

校验也补上**非退化判据** —— 只验「合法分区」是不够的，「不切」也是合法分区：
  章数 ≥ 2、最大章不超过总段数的一半、且不能所有段各自成章。

用法：python tools/hier-recursive-probe.py [文档 id，默认取一篇长的]
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
SQL_FILE = os.path.join(HERE, "_hr.sql")
CHAT = "qwen3:4b"

LEAF_PROMPT = """下面这段资料已按句子编号（1 到 N）。请按**语义**把它切成若干段，每段讲一件事。

只输出每段的**结束句编号**，JSON：{"ends":[3,7,12]}

要求：
- 编号从 1 开始、严格递增，**最后一项必须等于 N**
- 每段 2~8 句；不要把整段当成一段，也不要逐句切
- 不要改写、不要解释"""

GROUP_PROMPT = """下面是一篇技术文档切出的 {S} 段（每段一行：段号 + 开头若干字）。
请按**主题**把它们归并成章 —— 讲同一件事的相邻段放进同一章。

只输出每章的**结束段号**，JSON：{"ends":[4,9,15]}

要求：
- 段号从 1 开始、严格递增，**最后一项必须等于 {S}**
- 每章 3~12 段；**不要把整篇归成一章，也不要每段各成一章**
- 章节边界落在主题转换处（换了在讲什么），不是按段数平均分
- 不要改写、不要解释"""

LEAF_SCHEMA = {"type": "object",
               "properties": {"ends": {"type": "array", "items": {"type": "integer"}}},
               "required": ["ends"]}


BS = chr(92)


def psql(sql):
    with io.open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE], capture_output=True, env=env)
    return p.stdout.decode("utf-8", "replace")


def psql_rows(sql):
    """
    取多行文本必须走 COPY —— `-A` 是按**行**分隔记录的，而块的正文里就有换行，
    行分隔会把一个块冲成好几条（这个坑第三次踩了：它会把块数虚报成三倍多）。
    COPY 的 text 格式会把换行转义成 \\n。
    """
    f = os.path.join(HERE, "_hr.sql")
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


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


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
    return [s for s in out if s.strip()] or [text]


def ask(system, user, schema, num_ctx):
    t0 = time.time()
    r = post("/api/chat", {"model": CHAT, "stream": False,
                           "think": os.environ.get("KB_THINK") == "1", "format": schema,
                           "options": {"temperature": 0.1, "num_ctx": num_ctx},
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]})
    ms = int((time.time() - t0) * 1000)
    txt = r.get("message", {}).get("content", "")
    try:
        return json.loads(txt).get("ends", []), ms
    except Exception:
        m = re.search(r'"ends"\s*:\s*\[([^\]]*)\]', txt)
        return ([int(x) for x in re.findall(r"\d+", m.group(1))] if m else []), ms


def repair(ends, n):
    e = sorted({x for x in ends if isinstance(x, int) and 1 <= x <= n}) or [n]
    if e[-1] != n:
        e.append(n)
    return e


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    doc = sys.argv[1] if len(sys.argv) > 1 else None
    if not doc:
        r = psql("SELECT doc_id FROM chunks GROUP BY doc_id ORDER BY count(*) DESC LIMIT 1;")
        doc = r.strip().splitlines()[0].strip()
    name = psql(f"SELECT name FROM documents WHERE id='{doc}';").strip().splitlines()[0]
    chunks = psql_rows(f"SELECT content FROM chunks WHERE doc_id='{doc}' ORDER BY seq")
    print(f"文档：{name[:40]}　{len(chunks)} 块\n")

    # ① 叶子层：逐块平面切分（已验证可行的那一层）
    print("—— ① 叶子层（逐块切分）——")
    segs, t0 = [], time.time()
    for i, c in enumerate(chunks, 1):
        ss = sentences(c)
        if len(ss) < 3:
            segs.append(c)
            continue
        numbered = "\n".join(f"{j+1}. {s.strip()}" for j, s in enumerate(ss))
        ends, ms = ask(LEAF_PROMPT.replace("N", str(len(ss))), numbered, LEAF_SCHEMA, 16384)
        e = repair(ends, len(ss))
        st = 0
        for x in e:
            piece = "".join(ss[st:x]).strip()
            if piece:
                segs.append(piece)
            st = x
        print(f"  块 {i:>2}：{len(ss)} 句 → {len(e)} 段　{ms}ms")
    print(f"  共 {len(segs)} 段，耗时 {time.time()-t0:.0f}s")

    # ② 章层：把段当单位分组
    print("\n—— ② 章层（把段分组）——")
    listing = "\n".join(f"{i+1}. {s[:38].replace(chr(10), ' ')}" for i, s in enumerate(segs))
    ends, ms = ask(GROUP_PROMPT.replace("{S}", str(len(segs))), listing, LEAF_SCHEMA, 16384)
    e = repair(ends, len(segs))
    sizes, prev = [], 0
    for x in e:
        sizes.append(x - prev)
        prev = x
    print(f"  返回 {len(e)} 章，章段数 {sizes}，耗时 {ms}ms")

    # 非退化校验
    ok_part = e[-1] == len(segs) and all(a < b for a, b in zip(e, e[1:]))
    ok_nondeg = (len(e) >= 2 and max(sizes) <= len(segs) * 0.6 and len(e) <= len(segs) * 0.7)
    print(f"\n合法分区：{'✅' if ok_part else '❌'}　"
          f"非退化（≥2 章、最大章 ≤60% 总段、章数 ≤70% 总段）：{'✅' if ok_nondeg else '❌'}")
    if not ok_nondeg:
        print("  → 又是「不切」那个逃生口：合法但没真分")


if __name__ == "__main__":
    main()
