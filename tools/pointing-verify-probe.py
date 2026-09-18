# -*- coding: utf-8 -*-
"""
指向 + 向量验证：模型指认原文里的句子/词，向量模型验它站不站得住。

为什么指向与「产出」不同：前面所有失败（不切 / 凑数 / 恒选一侧 / 抄示例）都是
**产出内容**才有的形状。指向的字母表是位置，退化反而看得见（全指第一句、一个都没指）。

三方对照，各管一段：
  几何指向   纯代码：与块质心最像的那一句（用户不信的那种）
  模型指向   LLM：核心句号 + 关键术语（**要求逐字出现**，可机械校验）
  向量验证   bge-m3：判别式检验 —— 术语与本块的相似度 **减去** 它与 5 个随机块的相似度
             差为正 = 这个词真的标志这一块；为负 = 放哪都行的泛词

三个指标各自可证伪：逐字率、指向合法性、向量验证通过率。

用法：python tools/pointing-verify-probe.py [块数，默认 60]
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

POINT_PROMPT = """下面这段资料已按句子编号。请指出两件事，**所指的是原文里已经有的东西**：

1. 哪几句是这段的**核心**（把最实的内容讲出来的那几句），给出句号
2. 这段在讲哪几个**术语或关键词**（必须是资料里**逐字出现**的词，不要改写、不要翻译）

只输出 JSON：{"core":[3,4],"terms":["..."]}"""

SCHEMA = {"type": "object",
          "properties": {"core": {"type": "array", "items": {"type": "integer"}},
                         "terms": {"type": "array", "items": {"type": "string"}}},
          "required": ["core", "terms"]}
CJK = re.compile(r"[\u4e00-\u9fff]")


def psql_rows(sql):
    f = os.path.join(HERE, "_pv.sql")
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


def post(path, body, timeout=180, tries=3):
    """
    带重试。为什么需要：混跑 chat 与 embed 会让 Ollama 周期性卡住（实测两次超时，
    一次在第一次调用、一次在 30/60），而卡住是偶发的 —— 重试比排查便宜。
    """
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            print(f"    （{path} 第 {k+1} 次超时/失败：{type(e).__name__}）", flush=True)
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
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 60

    rows = psql_rows("SELECT id, doc_id, content FROM chunks ORDER BY md5(id::text) LIMIT " + str(want))
    chunks = []
    for line in rows:
        p = line.split("\t", 2)
        if len(p) == 3:
            chunks.append({"id": p[0], "doc": p[1], "text": p[2]})
    print(f"取样 {len(chunks)} 块\n")

    cv = embed([c["text"] for c in chunks])
    t0 = time.time()
    n_core_ok = n_core_bad = 0
    core_pct, term_margin, n_term = [], [], 0
    verbatim = 0
    llm_terms_all = []
    for i, c in enumerate(chunks, 1):
        ss = sentences(c["text"])
        if len(ss) < 2:
            continue
        d = post("/api/chat", {"model": CHAT, "stream": False,
                               "think": os.environ.get("KB_THINK") == "1", "format": SCHEMA,
                               "options": {"temperature": 0.1, "num_ctx": 16384},
                               "messages": [{"role": "system", "content": POINT_PROMPT},
                                            {"role": "user",
                                             "content": "\n".join(f"{j+1}. {s}" for j, s in enumerate(ss))}]})
        try:
            obj = json.loads(d.get("message", {}).get("content", ""))
        except Exception:
            obj = {}
        core = [x for x in obj.get("core", []) if isinstance(x, int) and 1 <= x <= len(ss)]
        terms = [t.strip() for t in obj.get("terms", []) if isinstance(t, str) and t.strip()]
        if not core:
            n_core_bad += 1
        else:
            n_core_ok += 1

        # 几何指向：与块质心最像的那一句
        sv = embed(ss)
        sims = sv @ cv[i - 1]
        geo = int(np.argmax(sims)) + 1

        # 向量验证核心句：被指的句子在全部句子里排第几（越靠前越"代表整块"）
        if core:
            for x in core:
                rank = 1 + int((sims > sims[x - 1]).sum())
                core_pct.append(rank / len(ss))
        # 与几何指向是否一致（允许 ±1 句）
        if core and any(abs(x - geo) <= 1 for x in core):
            verbatim += 1   # 复用这个计数器前先重置，见下

        # 前 3 块把两种指向并排打出来 —— 「最典型」与「最实质」未必同指，这只能读
        if i <= 3:
            print(f"\n  块{i}（{len(ss)} 句）")
            print(f"    模型指的核心：{[ss[x-1][:50] for x in core]}")
            print(f"    几何指的（离质心最近）：{ss[geo-1][:50]}")
            print(f"    该块首句：{ss[0][:50]}")

        # 向量验证术语：判别式 —— 与本块相似度 减去 与 5 个随机块的相似度
        if terms:
            tv = embed(terms)
            others = np.delete(np.arange(len(chunks)), i - 1)
            pick = np.random.default_rng(i).choice(others, size=min(5, len(others)), replace=False)
            own = tv @ cv[i - 1]
            cross = (tv @ cv[pick].T).mean(axis=1)
            for t, o, x in zip(terms, own, cross):
                term_margin.append(float(o - x))
                n_term += 1
                if t.lower() in c["text"].lower():
                    llm_terms_all.append(1)
                else:
                    llm_terms_all.append(0)
        # 边跑边打：长时间混跑 chat+embed 会让 Ollama 变慢甚至卡住，
        # 指标只在最后打印的话，一次超时就白跑（这个坑今天踩了两次）
        if i % 10 == 0 or i == len(chunks):
            cp = np.array(core_pct) if core_pct else np.array([0.5])
            tm = np.array(term_margin) if term_margin else np.array([0.0])
            print(f"  {i:>3}/{len(chunks)}  核心句百分位中位 {np.median(cp):.2f}　"
                  f"术语逐字率 {100*np.mean(llm_terms_all) if llm_terms_all else 0:.0f}%　"
                  f"判别差为正 {100*(tm > 0).mean():.0f}%", flush=True)

    print(f"\n耗时 {time.time()-t0:.0f}s")
    print(f"\n—— ① 模型指向的合法性 ——")
    print(f"  给出了核心句的块：{n_core_ok}/{n_core_ok+n_core_bad}")
    print(f"  术语**逐字出现在原文**的比例：{sum(llm_terms_all)}/{len(llm_terms_all)}"
          f" = {100*sum(llm_terms_all)/max(len(llm_terms_all),1):.0f}%"
          f"（可机械校验 —— 这是指向相对「产出」的第一处优势）")

    print(f"\n—— ② 向量验证核心句 ——")
    arr = np.array(core_pct)
    print(f"  被指的核心句在全部句子里的百分位：中位 {np.median(arr):.2f}"
          f"（0.1 = 排在最前面 10%）")
    print(f"  排进前 25% 的：{100*(arr <= 0.25).mean():.0f}%"
          f"　排进前 50% 的：{100*(arr <= 0.50).mean():.0f}%")

    print(f"\n—— ③ 向量验证术语（判别式：本块 − 随机块）——")
    m = np.array(term_margin)
    print(f"  术语 {len(m)} 个，判别差：中位 {np.median(m):+.3f}，"
          f"为正的占 {100*(m > 0).mean():.0f}%")
    print(f"  （为正 = 这个词与本块比与别的块更像 —— 即它确实标志这一块）")
    print(f"  差为负的（泛词，验证不通过）：{100*(m <= 0).mean():.0f}%")


if __name__ == "__main__":
    main()
