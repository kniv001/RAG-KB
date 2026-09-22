# -*- coding: utf-8 -*-
"""
**按规格文件出题** —— 出题的流水线化。

为什么要有它：要写到百题，靠"每次改一下 Python 里的 SPEC 列表"是走不远的 ——
而且这个项目里**用 heredoc 批量改中文/带转义的源码反复出事**（`\\n` 被吃成真换行、
中文字符串被截断，同一天三次）。把**题目规格**放进 JSON，就让"写题"和"改代码"
分开：写题只碰数据，不碰代码。

规格文件 `tools/cases/_spec/<主题>.json`：

    {
      "doc": "<文档名>",            // 该主题所属文档
      "prefix": "vd",               // 题号前缀
      "cases": [
        [序号, [块号...], "问句"],
        ...
      ]
    }

产物 `tools/cases/<名字>.json`（汇成一个题目集）：
    python tools/ruler/build_spec.py 百题                # 收 _spec 下全部
    python tools/ruler/build_spec.py 百题 向量数据库 JVM  # 只收指定主题

靶子摘录由 `C.head(i)` 生成 —— 与 `corpus.norm` 天然一致。
**手抄摘录不一致时不报错，只会让全中率莫名变低**，正是本项目吃过几次的坑。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
SPEC_DIR = os.path.join(TOOLS, "cases", "_spec")
sys.path.insert(0, TOOLS)

from ruler import corpus  # noqa: E402


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法：python tools/ruler/build_spec.py <输出名> [主题...]")
    out_name = sys.argv[1]
    want = sys.argv[2:]

    files = sorted(f for f in os.listdir(SPEC_DIR) if f.endswith(".json"))
    if want:
        files = [f for f in files if any(w in f for w in want)]
    if not files:
        raise SystemExit(f"{SPEC_DIR} 下没有匹配的规格")

    C = corpus.load()
    index = {}
    for i in range(C.n):
        index.setdefault(C.doc[i], {})[str(C.seq[i])] = i

    cases = []
    for f in files:
        spec = json.load(io.open(os.path.join(SPEC_DIR, f), encoding="utf-8"))
        doc, pre = spec["doc"], spec.get("prefix", f[:2])
        if doc not in index:
            raise SystemExit(f"！{f}：语料里没有这篇文档 —— {doc[:40]}")
        for n, seqs, q in spec["cases"]:
            targets = []
            for s in seqs:
                i = index[doc].get(str(s))
                if i is None:
                    raise SystemExit(f"！{pre}{n}：{doc[:20]} 里没有第 {s} 块")
                targets.append({"excerpt": C.head(i), "seq_hint": int(s)})
            cases.append({"id": f"{pre}{n:02d}", "doc": doc, "q": q, "targets": targets})
        print(f"  {f[:-5]:<12} {len(spec['cases']):>3} 题")

    out = {
        "schema": "ruler/cases@1",
        "name": out_name,
        "kind": "retrieval",
        "judge": "all-hit",
        "note": ("出题流水线的产物（tools/ruler/build_spec.py + tools/cases/_spec/*.json）。"
                 "判据：全中率 —— 答案需要的块是否全部进 top-k。靶子锚内容（excerpt），seq 只作提示。"),
        "cases": cases,
    }
    p = os.path.join(TOOLS, "cases", f"{out_name}.json")
    json.dump(out, io.open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n合计 {len(cases)} 题 → {p}")


if __name__ == "__main__":
    main()
