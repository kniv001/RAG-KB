# -*- coding: utf-8 -*-
"""
**"用 ctx 检索"值不值得做** —— 先量余量，别先写代码。

背景：`chunks.ctx`（每块一句「本段可回答：…？」）是**导入时**生成并**拼进块向量**的，
689/689 块都有；但它在**两条检索通道里都只被 SELECT、从来没被搜过** ——
关键词通道搜的是 `content ILIKE`，向量通道用的是拼过 ctx 的块向量。

所以问题是：**再加一条"搜 ctx"的通道，能捞回现在捞不到的靶子吗？**

做法（不跑整条管线，只量靶子层）：
  1. 取基准里每道题的**靶子块**（按 excerpt 在库里定位）；
  2. 用与 Java 侧同一套取词规则（ASCII 词 ≥2 + 中文二元组，上限 24）；
  3. 分别看靶子块的 `content` 与 `ctx` 命中了几个词 —— 用与关键词通道同一个门槛
     （至少 2 个词、且覆盖查询词的 25%）；
  4. 分四类：**只有 content 命中 / 只有 ctx 命中 / 都命中 / 都不命中**。
     "只有 ctx 命中"那一格就是这条通道的**余量**。

用法：
    python tools/ctx-channel-probe.py                # 默认 multihop-127
    python tools/ctx-channel-probe.py --bench multihop-25
"""
import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE))
from ruler import corpus  # noqa: E402

WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]{1,}")
CJK = re.compile(r"[一-鿿]")


def extract_terms(q):
    """与 Retriever.extractTerms 同一套规则 —— **必须一致**，否则量的是另一条通道。"""
    terms = []
    seen = set()

    def add(t):
        if t not in seen:
            seen.add(t)
            terms.append(t)

    for m in WORD.finditer(q):
        add(m.group())
    cjk = [c for c in CJK.findall(q)]
    for i in range(len(cjk) - 1):
        add(cjk[i] + cjk[i + 1])
    if len(cjk) == 1:
        add(cjk[0])
    return terms[:24]


def hits(text, terms):
    """命中几个词（大小写不敏感，子串匹配 —— 与 ILIKE 同语义）。"""
    if not text:
        return 0
    t = text.lower()
    return sum(1 for x in terms if x.lower() in t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="multihop-127")
    a = ap.parse_args()

    bench = json.load(io.open(os.path.join(HERE, "cases", a.bench + ".json"), encoding="utf-8"))
    cases = bench["cases"]
    print(f"基准 {a.bench}：{len(cases)} 题\n")

    # 先把靶子定位到块（用 excerpt 的前 24 字做子串匹配；规范形比对，去空白差异）
    chunks = corpus.psql_rows("SELECT id, content, coalesce(ctx,'') FROM chunks")
    norm_idx = {}
    for cid, content, ctx in chunks:
        norm_idx.setdefault(corpus.norm(content)[:60], []).append((cid, content, ctx))

    stat = {"只有 content": 0, "只有 ctx": 0, "都命中": 0, "都不命中": 0, "靶子没定位到": 0}
    examples = []
    total_targets = 0
    for cs in cases:
        q = cs["q"]
        terms = extract_terms(q)
        if not terms:
            continue
        need = max(2, int(len(terms) * 0.25 + 0.999) - 0)   # 与 Java 侧 ceil 对齐：至少 2 且 ≥25%
        need = max(2, -(-len(terms) * 25 // 100))
        found_any = False
        for t in cs.get("targets", []):
            ex = corpus.norm(t["excerpt"])[:24]
            if len(ex) < 8:
                continue
            hit_chunk = None
            for cid, content, ctx in chunks:
                if ex in corpus.norm(content):
                    hit_chunk = (cid, content, ctx)
                    break
            if not hit_chunk:
                stat["靶子没定位到"] += 1
                continue
            found_any = True
            total_targets += 1
            cid, content, ctx = hit_chunk
            hc, hx = hits(content, terms), hits(ctx, terms)
            okc, okx = hc >= need, hx >= need
            key = ("都命中" if okc and okx else
                   "只有 content" if okc else
                   "只有 ctx" if okx else "都不命中")
            stat[key] += 1
            if key == "只有 ctx" and len(examples) < 5:
                examples.append((q[:40], ctx[:60], terms[:6]))
        if not found_any:
            pass

    print(f"靶子总数 {total_targets}（另有 {stat['靶子没定位到']} 个没在库里定位到）")
    print(f"  都命中      {stat['都命中']}")
    print(f"  只有 content {stat['只有 content']}　← 现在的通道能捞到的")
    print(f"  **只有 ctx** {stat['只有 ctx']}　← **这条通道的余量**")
    print(f"  都不命中     {stat['都不命中']}　← 两条都捞不到（多半要换查询说法）")
    if examples:
        print("\n「只有 ctx 命中」的例子：")
        for q, ctx, ts in examples:
            print(f"  问：{q}")
            print(f"    ctx：{ctx}")
            print(f"    词：{'、'.join(ts)}")


if __name__ == "__main__":
    main()
