# -*- coding: utf-8 -*-
"""**基准的第四张网：覆盖率** —— 题**坏没坏**前两张网已经能答，这张答"题**该往哪长**"。

## 为什么要有它

前三张网（声明式 / 行为式 / 材料式）都在答"**这道题还成不成立**"。
它们答不了另一个问题：**库里有一大片，而一道题都没有** ——
而"基准随语料生长"的另一半正是这个（题坏掉只是它的一半）。

## 口径

**一道题的"地盘"** = 拿它的问句去语料里按相似度取 top-N（只认**过地板**的那些句），
这些句子所在的文档就是这道题够得着的地方。所有题的地盘并起来 = **被覆盖的文档**；
剩下的就是**空白**。

⚠️ 它是**指示器不是判决**（与 `bench-rot-probe` 同一条纪律）：
"没被任何题够到"**不等于**"该有题" —— 那篇文档可能就是不该问（模板、目录、一次性的通知）。
它的用法是**把空白列出来让"该不该扩题"有数据**，而不是替人决定。

用法：python tools/eval/bench-coverage-probe.py [--floor 0.65] [--top 20] [--show 25]
"""
import collections
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from ruler import corpus                                        # noqa: E402


def arg(name, dflt):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return dflt


def main():
    floor = float(arg("--floor", "0.65"))
    top = int(arg("--top", "20"))
    show = int(arg("--show", "25"))

    C = corpus.load()
    print(C.line())
    # 文档侧：块数 + 来源类型（`upload` 是用户自己的资料，`web` 是抓的，`feed` 是信息流晋升的）
    docs = {}
    for i in range(C.n):
        d = C.doc[i]
        e = docs.setdefault(d, {"n": 0, "kind": ""})
        e["n"] += 1
    for doc, kind in corpus.psql_rows(
            "SELECT name, source_kind FROM documents", tag="cov"):
        if doc in docs:
            docs[doc]["kind"] = kind

    cases = []
    for b in ("answer-quality", "news-live"):
        p = os.path.join(HERE, "benches", b + ".json")
        if os.path.exists(p):
            for c in json.load(io.open(p, encoding="utf-8"))["cases"]:
                cases.append((b, c))

    qv = corpus.embed([c["q"] for _, c in cases])
    print(f"题 {len(cases)} 道 · 地板 {floor} · 每题取 top-{top} 句\n", flush=True)

    territory = collections.defaultdict(set)      # doc → 够到它的题
    per_case = {}
    for (b, c), v in zip(cases, qv):
        lit = "[" + ",".join(f"{x:.7g}" for x in v) + "]"
        rows = corpus.psql_rows(
            "SELECT c.doc_id, c.seq, s.text, round((1-(s.embedding <=> '%s'::vector))::numeric,3) "
            "FROM sentences s JOIN chunks c ON c.id=s.chunk_id "
            "WHERE s.embedding IS NOT NULL AND s.kind <> 'head' "
            "AND 1-(s.embedding <=> '%s'::vector) >= %s "
            "ORDER BY s.embedding <=> '%s'::vector LIMIT %d" % (lit, lit, floor, lit, top),
            tag="cov2")
        names = {row[0]: row[1] for row in corpus.psql_rows(
            "SELECT id, name FROM documents", tag="cov3")}
        got = {names.get(r[0], r[0]) for r in rows}
        per_case[(b, c["id"])] = got
        for d in got:
            territory[d].add(f"{b}:{c['id']}")

    covered = set(territory)
    blank = [d for d in docs if d not in covered]
    n_blank_chunks = sum(docs[d]["n"] for d in blank)
    print(f"===== 覆盖 =====")
    print(f"  文档 {len(docs)} 篇 / 块 {C.n}")
    print(f"  被至少一道题够到：**{len(covered)} 篇**（{C.n - n_blank_chunks} 块）")
    print(f"  空白：**{len(blank)} 篇**（{n_blank_chunks} 块 = "
          f"{100.0 * n_blank_chunks / max(1, C.n):.0f}%）\n")

    bykind = collections.Counter(docs[d]["kind"] or "?" for d in blank)
    print("  空白按来源：" + "　".join(f"{k} {v}" for k, v in bykind.most_common()))
    print(f"\n  空白文档（按块数降序，最多列 {show}）：")
    for d in sorted(blank, key=lambda x: -docs[x]["n"])[:show]:
        print(f"    {docs[d]['n']:>3} 块  [{docs[d]['kind'] or '?':<6}]  {d[:62]}")

    print("\n  只被**一道**题够到的文档（最薄的那批）：")
    thin = sorted((d for d in covered if len(territory[d]) == 1),
                  key=lambda x: -docs[x]["n"])[:8]
    for d in thin:
        print(f"    {docs[d]['n']:>3} 块  {list(territory[d])[0]:<22}{d[:46]}")

    print("\n⚠️ 「没被够到」**不等于**「该有题」—— 它可能就不该问（模板/目录/一次性通知）。")
    print("   这张表的用法是**把空白列出来，让「该不该扩题」有数据**，不是替人决定。")


if __name__ == "__main__":
    main()
