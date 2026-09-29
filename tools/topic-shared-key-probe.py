# -*- coding: utf-8 -*-
"""
**「共享词当上一层」能压掉多少** —— 接 topic-layer2-probe 的结论。

那一节的结论是：**相似度层做不出上一层**（0.75~0.80 那一档真同族与假阳性混在一起，
单链一降阈值就链成一坨）。但亚运会 59 个议题/310 条**明明是一件事** ——
它们共享的不是"语义"，是**字面上的「亚运」两字**。

所以这一节量另一种上一层：**共享词**（≈ 实体/事件名的字面锚）。
   · 题名里到底有多少个"共享出来的词"？
   · 按共享词把议题并起来，能压成几个入口？覆盖多少议题/条目？
   · 会不会也链成一坨（"中国"这种词会把全库连起来）？

这是"实体抽取"那一块的**上界估计** —— 真正做实体要比这准，但形状一样。

用法：python tools/topic-shared-key-probe.py
"""
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402

# 语域词（谁都有，不指示"哪件事"）。**这份表是手写的**，加词要能说出理由：
# 它们是"新闻文体"的一部分，不是"这件事"的一部分。
STOP = {
    "中国", "我国", "全国", "中方", "国家", "记者", "报道", "举行", "举办", "启动",
    "发布", "召开", "推进", "加强", "提升", "实现", "表示", "指出", "强调", "介绍",
    "今年", "昨天", "今天", "明天", "近日", "日前", "目前", "以来", "同时", "以及",
    "记者会", "发布会", "工作", "有关", "方面", "问题", "情况", "活动", "发展", "建设",
    "习近平", "李强",  # 人名**单列**：他们是真实体，但会连掉太多东西（见下测）
}


def bar(n, mx, width=40):
    return "█" * max(0, min(width, int(round(n * width / mx)) if mx else 0))


def main():
    rows = corpus.psql_rows(
        "SELECT coalesce(label,'') , item_n FROM feed_topics WHERE label IS NOT NULL")
    labels = [(r[0], int(r[1])) for r in rows if len(r) >= 2 and r[0]]
    print("=" * 74)
    print(f"已命名议题 {len(labels)} 个 · 覆盖条目 {sum(n for _, n in labels)} 条")
    print("=" * 74)

    # ── 一、题名里的高频共享词 ──────────────────────────────────────────
    print("\n一、**共享词**（题名里出现 ≥2 次的 2~4 字串）")
    df = Counter()
    for lab, _ in labels:
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(lab) - n + 1):
                g = lab[i:i + n]
                if re.fullmatch(r"[一-鿿]+", g):
                    seen.add(g)
        for g in seen:
            df[g] += 1
    shared = [(g, c) for g, c in df.items() if c >= 2]
    shared.sort(key=lambda x: -x[1])
    print(f"   ≥2 次的串 {len(shared)} 个 · ≥3 次的 {sum(1 for _, c in shared if c >= 3)} 个")
    mx = shared[0][1] if shared else 1
    print(f"   {'词':<8}{'几个议题含它':>10}  覆盖")
    shown = set()
    k = 0
    for g, c in shared:
        if g in STOP:
            continue
        print(f"   {g:<8}{c:>10}  {bar(c, mx)}")
        shown.add(g)
        k += 1
        if k >= 30:
            break
    print(f"   ⚠️ 停用词表（不计入上表）：{'、'.join(sorted(STOP)[:14])}…")
    print("      —— 它们是「新闻文体」的一部分，不是「这件事」的一部分")

    # ── 二、按共享词并：能压成几个入口 ──────────────────────────────────
    print("\n二、**按共享词并簇**（单链；停用词不计，只看上表那一档之外的全部共享词）")
    print(f"   {'最短词长':>8}{'入口数':>8}{'最大入口':>10}{'第二大':>8}{'压掉的议题':>12}")
    for minlen in (2, 3, 4):
        parent = list(range(len(labels)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        owner = {}
        for i, (lab, _) in enumerate(labels):
            seen = set()
            for n in range(minlen, 5):
                for j in range(len(lab) - n + 1):
                    g = lab[j:j + n]
                    if not re.fullmatch(r"[一-鿿]+", g) or g in STOP:
                        continue
                    seen.add(g)
            for g in seen:
                if g in owner:
                    union(owner[g], i)
                else:
                    owner[g] = i
        sizes = Counter(find(i) for i in range(len(labels)))
        s = sorted(sizes.values(), reverse=True)
        print(f"   {minlen:>8}{len(s):>8}{s[0] if s else 0:>10}"
              f"{s[1] if len(s) > 1 else 0:>8}{len(labels)-len(s):>12}")
    print("   ⚠️ 若「最大入口」在 minlen=2 时接近全库，说明**共享词也会链**（同一个失败模式换了素材）")

    # ── 三、只看"够格的入口"：≥5 个议题的共享词 ─────────────────────────
    print("\n三、**够格的入口**（含 ≥5 个议题的共享词）—— 这些就是用户会点的「大类」")
    big = [(g, c) for g, c in shared if c >= 5 and g not in STOP]
    big.sort(key=lambda x: -x[1])
    cover_topics = 0
    cover_items = 0
    idx = defaultdict(list)
    for i, (lab, n) in enumerate(labels):
        for g, c in big:
            if g in lab:
                idx[g].append((lab, n))
                break
    print(f"   {'共享词':<10}{'议题数':>7}{'条目数':>7}")
    for g, c in big[:25]:
        it = sum(n for _, n in idx[g])
        cover_topics += c
        cover_items += it
        print(f"   {g:<10}{c:>7}{it:>7}")
    print(f"   ⇒ 前 25 个共享词覆盖 {cover_topics} 个议题 / {cover_items} 条条目")
    print(f"     （占已命名议题 {len(labels)} 的 {cover_topics/len(labels)*100:.0f}%，"
          f"占已命名议题条目 {sum(n for _,n in labels)} 的 "
          f"{cover_items/sum(n for _,n in labels)*100:.0f}%）")

    # ── 四、亚运会那一族：共享词的样板 ──────────────────────────────────
    print("\n四、样板：**共享词「亚运」**")
    ys = [(lab, n) for lab, n in labels if "亚运" in lab]
    print(f"   {len(ys)} 个议题 / {sum(n for _, n in ys)} 条条目 → 1 个入口")
    print("   ⇒ 这就是「上一层」该有的样子：**它不靠相似度，靠字面**（所以它能跨越语义）。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
