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

## 用法

    python tools/eval/bench-expand.py <基准> <候选文件.json|逗号分隔的问句>
    # 候选文件格式：[{"id":"nl-g9","q":"…"}, …]

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

TOP, FLOOR, MIN_HITS = 20, 0.65, 3


def territory(C, vec):
    lit = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
    return C.psql_rows(
        "SELECT c.id, c.doc_id FROM sentences s JOIN chunks c ON c.id=s.chunk_id "
        "WHERE s.embedding IS NOT NULL AND s.kind <> 'head' "
        "AND 1-(s.embedding <=> '%s'::vector) >= %s "
        "ORDER BY s.embedding <=> '%s'::vector LIMIT %d" % (lit, FLOOR, lit, TOP),
        tag="bx")


def top1(C, vec):
    lit = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
    r = C.psql_rows(
        "SELECT s.text FROM sentences s WHERE s.embedding IS NOT NULL AND s.kind <> 'head' "
        "ORDER BY s.embedding <=> '%s'::vector LIMIT 1" % lit, tag="bx2")
    return re.sub(r"\s+", "", r[0][0])[:30] if r else ""


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    bench, spec = sys.argv[1], sys.argv[2]
    if os.path.exists(spec):
        items = [(c["id"], c["q"]) for c in json.load(io.open(spec, encoding="utf-8"))]
    else:
        items = [(f"new{i+1}", q.strip()) for i, q in enumerate(spec.split("|")) if q.strip()]

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
    for (cid, q), vec in zip(items, corpus.embed([q for _, q in items])):
        if cid in ids:
            print(f"  ！{cid} 已存在，跳过")
            continue
        rows = territory(C, vec)
        if len(rows) < MIN_HITS:
            print(f"  ✗ {cid} 材料只有 {len(rows)} 句（< {MIN_HITS}）—— 不加：{q[:36]}")
            continue
        fresh = [r for r in rows if r[0] not in covered]
        if not fresh:
            print(f"  ✗ {cid} 没带来新覆盖 —— 不加：{q[:36]}")
            continue
        mf = top1(C, vec)
        b["cases"].append({
            "id": cid, "kind": "grounded", "q": q,
            "premise": {"material": "some", "must_find": [mf]},
            "note": "2026-09-29 扩题（bench-coverage-probe 的空白数据驱动 + bench-expand 两闸）；"
                    "must_find 取该题最像的那句前 30 字，是**变更探测器**不是标准答案。",
        })
        for r in fresh:
            covered.add(r[0])
        added += 1
        print(f"  ✓ {cid}  +{len(fresh)} 块新覆盖（材料 {len(rows)} 句）  {q[:34]}")
    io.open(p, "w", encoding="utf-8").write(json.dumps(b, ensure_ascii=False, indent=1) + "\n")
    print(f"\n⇒ {bench} 现在 {len(b['cases'])} 题（本批 +{added}）")
    print("   下一步：`python tools/eval.py audit` 过一遍前提核对")


if __name__ == "__main__":
    main()
