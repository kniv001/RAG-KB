# -*- coding: utf-8 -*-
"""
**候选块对查找器** —— 给多跳出题找材料。

要出的题长什么样（从「为什么 72% 的题不贡献区分度」倒推）：
  **两块以上、且至少有一块按字面**（顺着问句的词）**找不回来** ——
  一眼能匹配到的块不构成多跳，那种题在每档都满分，没有区分度。

所以找的是**互相似高、字面重叠低**的块对：
  · 互相似高 ⇒ 语义上确实相关（同一件事的两个侧面），能问成一个问题
  · 字面重叠低 ⇒ 按问句的词只能捞到其中一块，另一块要靠语义
  · 同篇 + 块号远 ⇒ 跨小节，不是相邻的两段

用法：python tools/ruler/pair_finder.py [对数，默认 15] [--doc 关键字]
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ruler import corpus  # noqa: E402

STOP = set("的了吗呢和与及或在是有为对从把被这那你我他它一个如何什么怎么哪些为什"
           "么样可以需要应该会能要不就都也很更最把给让使")


def toks(s, n=2):
    """字面重叠用**字符二元组**（中文没有空格，二元组比词切分稳）。"""
    t = re.sub(r"\s+", "", s)
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want = int(args[0]) if args else 15
    doc_filter = None
    if "--doc" in sys.argv:
        doc_filter = sys.argv[sys.argv.index("--doc") + 1]

    C = corpus.load()
    V = C.vecs("ctx+body")
    T = [toks(b) for b in C.body]

    # **先滤掉"对什么都像"的噪声块** —— 第一版没滤，前 8 对里有 7 对是
    # nginx 访问日志和页头来源行：它们余弦高（内容泛）而字面重叠低（词都不同），
    # 于是霸占排序。**它们不是"相关"，是"没有信息"。**
    # 判据用两条最朴素的：正文够长 + 中文占比够高。
    def is_content(i):
        b = C.body[i] or ""
        if len(b) < 150:
            return False
        zh = sum(1 for ch in b if "一" <= ch <= "鿿")
        return zh / max(1, len(b)) >= 0.45

    idx = [i for i in range(C.n)
           if (doc_filter is None or doc_filter in C.doc[i]) and is_content(i)]
    print(f"（内容块 {len(idx)}/{C.n} —— 滤掉了日志/页头/脚注那类）")

    # **同篇内两两比**（跨篇的已被 xdoc-8 证明会饱和，先不做）
    cands = []
    for a_pos, i in enumerate(idx):
        for j in idx[a_pos + 1:]:
            if C.doc[i] != C.doc[j]:
                continue
            try:
                si, sj = int(C.seq[i]), int(C.seq[j])
            except ValueError:
                continue
            gap = abs(si - sj)
            if gap < 8:                       # 太近 ⇒ 多半同一个小节
                continue
            cos = corpus.cos(V[i], V[j])
            if cos < 0.55:                    # 不够相关
                continue
            ov = len(T[i] & T[j]) / max(1, min(len(T[i]), len(T[j])))
            if ov > 0.35:                     # 字面太像 ⇒ 一块捞到另一块也跟着来
                continue
            cands.append((cos - ov, cos, ov, gap, i, j))

    cands.sort(reverse=True)
    print(f"候选 {len(cands)} 对，取前 {want}（按 相似 − 字面重叠 排序）\n")
    # `--chars N` 多打点正文 —— 出题时要读全，96 字不够
    nch = 96
    if "--chars" in sys.argv:
        nch = int(sys.argv[sys.argv.index("--chars") + 1])
    _ws = re.compile(r"\s+")
    for score, cos, ov, gap, i, j in cands[:want]:
        print(f"── cos={cos:.2f} 字面重叠={ov:.2f} 相隔{gap}块　{C.doc[i][:34]}")
        print(f"   A#{C.seq[i]:<4} {_ws.sub(' ', C.body[i])[:nch]}")
        print(f"   B#{C.seq[j]:<4} {_ws.sub(' ', C.body[j])[:nch]}")
        print()


if __name__ == "__main__":
    main()
