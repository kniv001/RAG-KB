# -*- coding: utf-8 -*-
"""
**合并题目集** —— 把几个来源合成一个（题号会重编，带来源前缀）。

为什么要单独一步而不是直接 `cat`：**题目集的名字就是它的身份**。
`multihop-25` 这个名字下的历史数字（56/68/84）只对那 25 题成立；
往里加题而不改名，历史数字就**失去可比性而看不出来** —— 这正是
「一个数字必须带尺子名 + 语料戳」那条规矩的由来。

所以：**新内容一律新名字**，合并时把每题标上来源。

用法：python tools/ruler/merge_cases.py <输出名> <题目集1> <题目集2> ...
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ruler import corpus  # noqa: E402


def main():
    if len(sys.argv) < 3:
        raise SystemExit("用法：python tools/ruler/merge_cases.py <输出名> <题目集...>")
    out_name, names = sys.argv[1], sys.argv[2:]

    C = corpus.load()
    index = {}
    for i in range(C.n):
        index.setdefault(C.doc[i], {})[str(C.seq[i])] = i

    cases, stat = [], []
    for nm in names:
        p = os.path.join(TOOLS, "cases", f"{nm}.json")
        src = json.load(io.open(p, encoding="utf-8"))
        kept = 0
        for c in src["cases"]:
            ts = []
            ok = True
            for t in c["targets"]:
                # 靶子按**内容**重新解析一遍：源文件若与当前语料不一致，会在这一步现形
                hit = C.find(t["excerpt"], c.get("doc"))
                if not hit:
                    ok = False
                    break
                ts.append({"excerpt": t["excerpt"], "seq_hint": C.seq[hit[0]]})
            if not ok:
                print(f"  ⚠ {nm}/{c['id']} 的靶子在当前语料里解析不到 —— 跳过")
                continue
            cases.append({"id": f"{nm[:4]}-{c['id']}", "doc": c["doc"],
                          "q": c["q"], "targets": ts, "src": nm})
            kept += 1
        stat.append((nm, kept, len(src["cases"])))

    out = {
        "schema": "ruler/cases@1",
        "name": out_name,
        "kind": "retrieval",
        "judge": "all-hit",
        "note": ("合并自 " + " + ".join(f"{n}({k}/{t})" for n, k, t in stat)
                 + "。**新名字 = 新身份** —— 历史数字只对旧名字成立，别混用。"),
        "cases": cases,
    }
    p = os.path.join(TOOLS, "cases", f"{out_name}.json")
    json.dump(out, io.open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for n, k, t in stat:
        print(f"  {n:<16}{k:>3}/{t:<3} 题")
    print(f"\n合计 {len(cases)} 题 → {p}")


if __name__ == "__main__":
    main()
