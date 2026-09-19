# -*- coding: utf-8 -*-
"""
reranker 验证：**池子扩到 60，再用模型精排**，看多跳全中率能提多少。

为什么现在才做：休眠清单里 reranker 那条写的是「判死，**但这是推理不是实测**：
前提是只精排现有 top-30」。多跳尺子给出了前提与判据 ——
k=60 时 12/12 道题的靶子**全在池中**，缺的只是"把它们提到前面"。

做法：
  · 池子 = 向量检索 top-60（与线上同一套文本：ctx + 正文）
  · 精排 = 4b 分批 listwise，每批 10 个候选，**要求给出完整排列**（不设"都不相关"的出口 ——
    今天实测：任何允许不做的口子都会被用满）
  · 批间合并：按轮转取（第 i 批的第 1 名、第 2 批的第 1 名…），保留批内次序
  · 判据 = 多跳**全中率**（答案要的每一块都进 top-12），与不精排的 50% 比

提示词里不出现任何可抄的字面编号（今天实测：写了 `{"at": 3}` 就五次全答 3）。

用法：python tools/rerank-probe.py [池子,默认60] [精排后取前几,默认12] [批大小,默认10]
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
EMBED = "bge-m3"
CHAT = os.environ.get("KB_RERANK_MODEL", "qwen3:4b")
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
QFILE = os.path.join(HERE, "multihop-questions.json")
VCACHE = os.path.join(HERE, "_corpus_vecs.json")

# **给分数，不给次序**。第一版让模型给"排列"，然后按批轮转合并 —— 那是错的：
# 批是池子的**分层**（第0批=1~10名、第5批=51~60名），轮转等于把每批前 2 名拼起来，
# 结果 top-12 = 池子的 {1,2,11,12,21,22,31,32,41,42,51,52} —— 把向量排 3~10 名的候选全踢了，
# 全中率直接掉到 0。**跨批只有"分数"能合，次序不能合。**
PROMPT = """下面是一个问题，和若干**编号候选片段**。

请给**每一个候选片段**打一个 0~9 的相关度分：与问题越相关分越高，完全无关给 0。

只输出 JSON，字段 scores，是一个数组，每一项形如 {"i": 编号, "s": 分数}。
**每个编号都要出现且只出现一次**，不要漏。

（片段是从文档里截断出来的，判断只依据看到的内容。）"""

SCHEMA = {"type": "object",
          "properties": {"scores": {"type": "array", "items": {
              "type": "object",
              "properties": {"i": {"type": "integer"}, "s": {"type": "integer"}},
              "required": ["i", "s"]}}},
          "required": ["scores"]}


def psql_rows(sql):
    f = os.path.join(HERE, "_rr.sql")
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
        for a, b in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(a, b)
        out.append(line)
    return out


def embed(texts):
    out = []
    for i in range(0, len(texts), 8):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": EMBED, "input": texts[i:i + 8]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            out += json.load(r)["embeddings"]
    return out


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)


def corpus():
    """全库块 + 嵌入（带缓存：改了语料才重算）"""
    rows = psql_rows("SELECT id, coalesce(ctx,''), content FROM chunks ORDER BY id")
    ids, texts = [], []
    for r in rows:
        p = r.split("\t")
        if len(p) < 3:
            continue
        ids.append(p[0])
        texts.append((p[1] + "\n" + p[2]) if p[1] else p[2])
    if os.path.exists(VCACHE):
        d = json.load(io.open(VCACHE, encoding="utf-8"))
        if d.get("ids") == ids:
            return ids, texts, d["vecs"]
    vecs = embed(texts)
    io.open(VCACHE, "w", encoding="utf-8").write(
        json.dumps({"ids": ids, "vecs": vecs}))
    return ids, texts, vecs


def llm_scores(question, cands):
    """cands: [(全局下标, 片段文本)]；返回 [(分数, 全局下标)]（批内）"""
    lines = [f"{i+1}. {t[:350]}".replace("\n", " ") for i, (_, t) in enumerate(cands)]
    body = {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.1, "num_ctx": 8192},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content":
                          f"问题：{question}\n\n候选片段：\n" + "\n".join(lines)}]}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        raw = json.load(r).get("message", {}).get("content", "")
    got = {}
    try:
        for it in json.loads(raw).get("scores", []):
            i, s = int(it.get("i", 0)), int(it.get("s", 0))
            if 1 <= i <= len(cands):
                got[i] = s
    except Exception:
        pass
    # 没给分的按 0 处理（不丢候选，只是排后面）
    return [(got.get(i + 1, 0), g) for i, (g, _) in enumerate(cands)]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    pool = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    take = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    bs = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    cfg = json.load(io.open(QFILE, encoding="utf-8"))
    bounds = json.load(io.open(os.path.join(HERE, "_bounds_chunking.json"),
                               encoding="utf-8"))["bounds"]
    segs = json.load(io.open(os.path.join(HERE, "..", "data", "_bintree-segs.json"),
                             encoding="utf-8"))["segs"]
    ids, texts, vecs = corpus()
    pos = {cid: i for i, cid in enumerate(ids)}
    print(f"全库 {len(ids)} 块　池子 {pool}　精排后取 {take}　批大小 {bs}\n")

    norm = lambda s: re.sub(r"\s+", "", s)

    def seg2chunk(seg):
        k = 0
        for i, b in enumerate(bounds):
            if seg >= b:
                k = i + 1
        return k

    docids = [x.strip() for x in psql_rows(
        "SELECT id FROM chunks WHERE doc_id=(SELECT id FROM documents WHERE name="
        f"'{cfg['doc']}') ORDER BY seq") if x.strip()]

    def accept(seg):
        key = norm(segs[seg - 1])[:40]
        hits = [pos[c] for c in docids if len(key) >= 16 and key in norm(texts[pos[c]])]
        if not hits:
            ci = seg2chunk(seg)
            hits = [pos[docids[ci]]] if ci < len(docids) else []
        return hits

    base_hit = rr_hit = 0
    n = 0
    for c in cfg["cases"]:
        want = [accept(s) for s in c["targets"]]
        qv = embed([c["q"]])[0]
        ranked = [j for _, j in sorted(((cos(qv, v), j) for j, v in enumerate(vecs)),
                                       reverse=True)[:pool]]
        b12 = ranked[:take]
        b_ok = all(any(j in b12 for j in grp) for grp in want)

        # 分批打分 → **全局按分数排**（同分按向量名次，名次即池子内的位置）
        batches = [ranked[i:i + bs] for i in range(0, len(ranked), bs)]
        scored = []
        for bt in batches:
            scored += llm_scores(c["q"], [(g, texts[g]) for g in bt])
        vrank = {g: i for i, g in enumerate(ranked)}
        merged = [g for _, g in sorted(scored, key=lambda x: (-x[0], vrank[x[1]]))]
        r12 = merged[:take]
        r_ok = all(any(j in r12 for j in grp) for grp in want)

        base_hit += b_ok
        rr_hit += r_ok
        n += 1
        mark = ("✅" if r_ok else "❌") + ("（原本就中）" if b_ok else "")
        print(f"  {mark} 精排后靶 {sum(1 for grp in want if any(j in r12 for j in grp))}/{len(want)}"
              f"　{c['q'][:40]}")
        sys.stdout.flush()

    print(f"\n—— 结果（池子 {pool}，取前 {take}）——")
    print(f"  不精排：全中率 {base_hit}/{n} = {100*base_hit/n:.0f}%")
    print(f"  **精排后：全中率 {rr_hit}/{n} = {100*rr_hit/n:.0f}%**")
    print(f"  差：{100*(rr_hit-base_hit)/n:+.0f} 点")


if __name__ == "__main__":
    main()
