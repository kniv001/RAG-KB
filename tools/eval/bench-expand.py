# -*- coding: utf-8 -*-
"""**扩题工具** —— 用覆盖率数据驱动，两道闸，`must_find` 从语料自动取。

## 两道闸（都比"问句相似度"更有意义）

  ① **材料**：过 0.65 的句子 **≥3**
     ⚠️ 第一版闸是"≥1"，**太松** —— 实测两条反例：材料 1~2 句时模型直接答
     「知识库中没有…的资料」（`aq-g9` 1 句、`aq-g16` 2 句）⇒ 那**不成其为 grounded 题**。
     「≥3」不是我定的：`AgenticRagService` 里 **`alive >= 3` 才算"有材料"**
     （决定要不要让模型先引用），用的是项目**既有的那条门槛**。
     ⚠️ 另：**问句越聚焦，相似度越高**（RAFT 那题 0.648 → 0.709 只是改了问法）；
     反过来**贴线的题**（0.60~0.65）成片存在 —— 见
     `2026-09-29-地板把部分可答那一档抹掉了四成`。
  ② **带来新覆盖**：它的地盘里**至少有一块**是现有题够不到的。
     这才是扩题的**目的**。（第一版用"与现有题的问句相似度 <0.62"当闸，**太粗**：
     它把"同一事件、不同事实"也判成重复 —— 而 `nl-p3` 是个宽泛的亚运题，
     任何亚运题都会像它。）

`must_find` 取**该题最像的那句**的前 30 字：按构造一定在库里，
而它的作用是**变更探测器**（库里没有了 ⇒ 前提破了），**不是"标准答案"**。

## 两种**目的**（闸跟着目的走）

扩题有两种完全不同的目的，而**闸不能一套用到底**（2026-09-29 实测被拦出来的）：

  · `--purpose cover`（默认）**补空白**：库里有一片没被任何题够到
    ⇒ 要的是"**带来新覆盖**"
  · `--purpose type` **扩题型**：库里已有题，但要一种**新的问法/形状**
    （如跨文档 `multi`）⇒ 要的是"**新形状**"，而它**常常不带来新覆盖**
    （实测 `nl-m4` 就是这么被误拦的）

材料那道闸**两种都适用**（任何题都要能被检索到，否则不成其为题）。

## 用法

    python tools/eval/bench-expand.py <基准> <候选文件.json> [--purpose cover|type]
    # 候选文件格式：[{"id":"nl-g9","q":"…","kind":"multi"}, …]

写完**跑一遍 `python tools/eval.py audit`**：前提核对会当场抓出"摘录写错/题写歪"。
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from ruler import corpus                                        # noqa: E402
from ruler.corpus import psql_rows                               # noqa: E402

TOP, FLOOR, MIN_HITS = 20, 0.65, 3


def territory(C, vec):
    lit = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
    return psql_rows(
        "SELECT c.id, c.doc_id FROM sentences s JOIN chunks c ON c.id=s.chunk_id "
        "WHERE s.embedding IS NOT NULL AND s.kind <> 'head' "
        "AND 1-(s.embedding <=> '%s'::vector) >= %s "
        "ORDER BY s.embedding <=> '%s'::vector LIMIT %d" % (lit, FLOOR, lit, TOP),
        tag="bx")


def docs_of(rows):
    """这块地盘**跨了几篇文档** —— `multi`（跨文档）这个题型的**机械定义**。

    为什么要它：题型名叫"跨文档"，可"跨没跨"此前**只是我说了算**。
    一张表 20 块全在同一篇里，那它就是单文档题，标 `multi` 是**骗自己**
    （判据里那条"覆盖了所有靶事实"也就退化了）。
    """
    return {d for _cid, d in rows}


def top1(C, vec):
    lit = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
    r = psql_rows(
        "SELECT s.text FROM sentences s WHERE s.embedding IS NOT NULL AND s.kind <> 'head' "
        "ORDER BY s.embedding <=> '%s'::vector LIMIT 1" % lit, tag="bx2")
    return re.sub(r"\s+", "", r[0][0])[:30] if r else ""


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    bench, spec = sys.argv[1], sys.argv[2]
    purpose = "cover"
    if "--purpose" in sys.argv:
        purpose = sys.argv[sys.argv.index("--purpose") + 1]
    if os.path.exists(spec):
        items = [(c["id"], c["q"], c.get("kind", "grounded"))
                 for c in json.load(io.open(spec, encoding="utf-8"))]
    else:
        items = [(f"new{i+1}", q.strip(), "grounded")
                 for i, q in enumerate(spec.split("|")) if q.strip()]

    C = corpus.load()
    print(C.line())
    p = os.path.join(HERE, "benches", bench + ".json")
    b = json.load(io.open(p, encoding="utf-8"))
    ids = {c["id"] for c in b["cases"]}
    covered = set()
    for vec in corpus.embed([c["q"] for c in b["cases"]]):
        for cid, _doc in territory(C, vec):
            covered.add(cid)
    print(f"{bench}：现有 {len(b['cases'])} 题，地盘 {len(covered)} 块\n")

    added = 0
    for (cid, q, kind), vec in zip(items, corpus.embed([q for _, q, _ in items])):
        if cid in ids:
            print(f"  ！{cid} 已存在，跳过")
            continue
        rows = territory(C, vec)
        if len(rows) < MIN_HITS:
            print(f"  ✗ {cid} 材料只有 {len(rows)} 句（< {MIN_HITS}）—— 不加：{q[:36]}")
            continue
        ndocs = len(docs_of(rows))
        if kind == "multi" and ndocs < 2:
            print(f"  ✗ {cid} 地盘只有 {ndocs} 篇文档 —— 自称 multi 却没跨文档：{q[:32]}")
            continue
        fresh = [r for r in rows if r[0] not in covered]
        if not fresh and purpose == "cover":
            print(f"  ✗ {cid} 没带来新覆盖 —— 不加：{q[:36]}")
            continue
        if not fresh:
            print(f"  · {cid} 没带来新覆盖，但 purpose=type ⇒ 只看形状")
        mf = top1(C, vec)
        b["cases"].append({
            "id": cid, "kind": kind, "q": q,
            "premise": {"material": "some", "must_find": [mf]},
            "note": "2026-09-29 扩题（bench-coverage-probe 的空白数据驱动 + bench-expand 两闸）；"
                    "must_find 取该题最像的那句前 30 字，是**变更探测器**不是标准答案。",
        })
        for r in fresh:
            covered.add(r[0])
        added += 1
        print(f"  ✓ {cid}  +{len(fresh)} 块新覆盖（材料 {len(rows)} 句 / {ndocs} 篇）  {q[:34]}")
    io.open(p, "w", encoding="utf-8").write(json.dumps(b, ensure_ascii=False, indent=1) + "\n")
    print(f"\n⇒ {bench} 现在 {len(b['cases'])} 题（本批 +{added}，purpose={purpose}）")
    print("   下一步：`python tools/eval.py audit` 过一遍前提核对")


if __name__ == "__main__":
    main()
