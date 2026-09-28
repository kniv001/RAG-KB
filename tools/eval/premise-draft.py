# -*- coding: utf-8 -*-
"""**给题面提"前提"的候选** —— 作者只需在候选里挑，不用凭空想。

## 前提是什么（见 `runner.audit_bench`）

答案侧的题此前**只有一句问句**，没有任何东西锚在语料上 ⇒ 语料长大后前提悄悄失效
（`aq-p4`：库里 09-17 就有的文档写着答案，而题 09-21 才写成"库里没有那个值"）。
前提就是**把那个隐含的假设写下来**，好让 `audit` 机械地核对：

    "premise": {"must_find": ["…"], "breaks_if_found": ["…"]}

## 本工具提两类候选（**纯机械，不请模型**）

  · **`must_find` 候选** = 与问题最像的那几句 —— "有相关内容"那一半的落点
  · **`breaks_if_found` 候选** = 与问题最像 **且带具体值** 的那几句 —— 答案若进了库，
    多半就长这样（`_VALUE` 那套"数字 + 单位"的形状，与判据同一套取值规则）

⚠️ **它只提候选，判断还是人的**：哪个句子真的"回答了问题"是语义判断，
机械办法分不出来（`tools/eval/cite-fp-probe.py` 那次量到过：判据能机械判的是
"值在不在"，不是"这值是不是答案"）。所以最后一步是**人看一眼点头**。

用法：python tools/eval/premise-draft.py answer-quality [--only aq-p4,aq-p1] [--top 6]
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

# 与判据同一族的"值"形状（见 judges._VALUE / _UNIT_STRICT）——
# **候选里带值的那些才是"答案候选"**，不带值的只是"相关但没答"。
_VALUEISH = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?[ \t]*[一-鿿A-Za-z%]{0,3}")

# 不需要前提的题型（与语料无关）
SKIP_KINDS = ("chitchat", "capability")


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "answer-quality"
    top = 12          # 每条"值候选"最多列几条
    floor = 0.66      # **带值的句子**只列过这个地板的（与生产的材料地板同量级）
    only = ""
    rest = sys.argv[2:]
    for i, a in enumerate(rest):
        if a == "--top":
            top = int(rest[i + 1])
        if a == "--floor":
            floor = float(rest[i + 1])
        if a == "--only":
            only = rest[i + 1]
    keys = [x.strip() for x in only.split(",") if x.strip()]

    bench = json.load(io.open(os.path.join(HERE, "benches", name + ".json"), encoding="utf-8"))
    cases = [c for c in bench["cases"] if c.get("kind") not in SKIP_KINDS]
    if keys:
        cases = [c for c in cases if c["id"] in keys]

    C = corpus.load()
    print(f"{name}：要提前提的 {len(cases)} 题（跳过 {'/'.join(SKIP_KINDS)}）· {C.line()}\n")
    qv = corpus.embed([c["q"] for c in cases])
    print(f"问题向量 {len(qv)} 条\n", flush=True)

    for c, v in zip(cases, qv):
        lit = "[" + ",".join(f"{x:.7g}" for x in v) + "]"
        # **一次拿 60 条**（不是 top-5）：因为"答案句"常常排在**第 6 名开外** ——
        # 这正是生产里那个"共享窗口"的形状（`aq-p4` 的「大约 1KB」是 0.6817，
        # 排在 0.736/0.722/0.722/0.712/0.711 后面）。只列 top-5 就会**看不见它**。
        rows = corpus.psql_rows(
            "SELECT c.doc_id, c.seq, s.seq, round((1-(s.embedding <=> '%s'::vector))::numeric,3), "
            "s.text FROM sentences s JOIN chunks c ON c.id=s.chunk_id "
            "WHERE s.embedding IS NOT NULL AND s.kind <> 'head' "
            "AND NOT (s.text LIKE '%%目录%%' AND length(s.text) > 80) "
            "ORDER BY s.embedding <=> '%s'::vector LIMIT 60" % (lit, lit), tag="pd")
        top3 = [r for r in rows[:3]]
        valued = [r for r in rows if _VALUEISH.findall(r[4] or "") and float(r[3]) >= floor][:top]
        print(f"=== {c['id']}　[{c['kind']}]　{c['q'][:46]}")
        print("  【must_find 候选】最像的几句（挑一条逐字抄）")
        for doc, cseq, sseq, sim, text in top3:
            print(f"    {sim}  {re.sub(r'\\s+', ' ', text or '').strip()[:66]}")
        print(f"  【breaks_if_found 候选】**带值且 ≥{floor}** 的（挑那些*真的回答了问题*的）")
        if not valued:
            print("    （一条都没有 —— 那这道题的前提目前是**成立**的）")
        for doc, cseq, sseq, sim, text in valued:
            t = re.sub(r"\s+", " ", text or "").strip()
            print(f"    {sim}  ★{_VALUEISH.findall(t)[:3]}  {t[:60]}")
        print()


if __name__ == "__main__":
    main()
