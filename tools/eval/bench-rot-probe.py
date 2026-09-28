# -*- coding: utf-8 -*-
"""**基准会随语料腐烂** —— 这个探针量它。

## 为什么（2026-09-29 发现）

`grounded-partial` 这一类的定义是「库里**有相关内容、但没有那个具体值**」，
判据是**两个合法出口**：① 资料里有 ⇒ 引用它；② 资料里没有 ⇒ 如实声明 + 标注通用知识。

而**语料是会长大的**（用户上传、联网抓取）。一旦那篇写着答案的文档进来了，
这道题就不再测它声称要测的东西 —— 它会从"难题"变成"普通题"，
而**没有任何东西会报错**：分数照出，标签照旧。
（与本项目反复吃的亏同族：**仪器陈旧时不报错，只让读数悄悄变味**。）

## 判据（机械、便宜、不需要模型）

对这一类的每一道题，统计历史落盘里模型**声明"知识库没有"的比例**：

  · 比例**高**（≈90%+）⇒ 模型也认为库里没有 ⇒ **标签成立**
  · 比例**低**（≈0）  ⇒ 模型几乎总在**引用** ⇒ **库里多半已经有那个值** ⇒ 标签腐烂

⚠️ 这是**指示器不是判决**：模型也可能该声明而没声明（那正是这个类要抓的行为）。
所以它只用来**挑出可疑的题**，判它到底还成不成立要人看。

## 用法

    python tools/eval/bench-rot-probe.py [bench…]
"""
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from eval import judges                                          # noqa: E402

# 需要"库里没有那个值"这个前提才成立的题型
ROT_KINDS = ("grounded-partial",)


def main():
    benches = sys.argv[1:] or ["answer-quality", "news-live"]
    for b in benches:
        path = os.path.join(HERE, "benches", b + ".json")
        if not os.path.exists(path):
            continue
        bench = json.load(io.open(path, encoding="utf-8"))
        qs = {c["id"]: (c.get("q") or "") for c in bench["cases"]}
        kinds = {c["id"]: c.get("kind") for c in bench["cases"]}
        stat = {}
        for f in sorted(glob.glob(os.path.join(HERE, "_runs", f"{b}__*.json"))):
            if f.endswith(".polluted"):
                continue
            try:
                d = json.load(io.open(f, encoding="utf-8"))
            except Exception:
                continue
            for x in (d.get("results") or []):
                i = x.get("id")
                if kinds.get(i) not in ROT_KINDS or i not in qs:
                    continue
                if not (x.get("answer") or "").strip():
                    continue
                s = stat.setdefault(i, [0, 0, 0])       # [次数, 通过, 声明没有]
                s[0] += 1
                r = judges.judge(x.get("kind"), x["answer"], x.get("sources") or [],
                                 x.get("q") or "", (x.get("stats") or {}).get("cites"))
                s[1] += 0 if [k for k in judges.PASS[x["kind"]] if r.get(k) is False] else 1
                s[2] += 1 if judges.declares_missing(x["answer"]) else 0
        if not stat:
            continue
        print(f"\n===== {b} · 需要「库里没有那个值」这一前提的题 =====")
        print(f"{'题':<8}{'问的是':<34}{'次数':>5}{'通过':>9}{'声明没有':>10}{'':>4}")
        for i, (n, ok, miss) in stat.items():
            rate = miss / max(1, n)
            flag = "  ← 可疑：模型几乎总在引用，库里多半已经有那个值" if rate < 0.3 else ""
            print(f"{i:<8}{qs[i][:32]:<34}{n:>5}{ok:>6}({100*ok//max(1,n)}%){miss:>7}({100*rate:.0f}%){flag}")
    print("\n读法：『声明没有』那一列**低** ⇒ 该题的前提可能已经不成立（语料长大了）。"
          "\n     它是**指示器**：挑出可疑的题，判它成立与否要人看那一题的材料。")


if __name__ == "__main__":
    main()
