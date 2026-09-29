# -*- coding: utf-8 -*-
"""
**「拿签名把议题收成入口」—— 逐日回放，看入口数稳不稳**（用户 2026-09-29：「实验下」）。

上一节的提议是：**签名 = 出现在该议题 ≥ 半数成员标题里的那几个词**（不是"共享即同族"，
所以**不做传递闭包** ⇒ 绕开今晨那个"最短词长 2 单链 → 最大入口 1870"的坑）。
它要干的是「爱知·名古屋亚运会」这一档的事：**把 69 个亚运议题收成 1 个入口**。

要回答的是"**入口数会不会稳定**"。语料只有 6 天，等不起 —— 所以**回放**：
对每个日期 D，只用 `fetched_at <= D` 的成员重建一次签名，看曲线的**形状**。

四条读数（前两条是主，后两条是风险）：
  一、**入口数 vs 议题数**的日曲线，以及 **新入口/新条目** —— 与议题层的 **0.577** 对照
  二、**签名的稳定性**：同一个议题，D 天的签名与最后一天一致吗（随成员增多会不会漂）
  三、**单条议题的归宿**：81% 的议题是单条，它们有多少**能落进已有入口**
  四、**入口自身的分裂/合并**：同一个入口的签名会不会今天一个样明天另一个样

⚠️ **只读**：不碰生产代码、不调模型；家具词**按分布定**（不手写 —— 手写那次把
「界面新闻」漏成了最大撞车主语，25 个议题撞在一起）。

用法：python tools/topic-entry-replay-probe.py [--top 12]
"""
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402

CJK = re.compile(r"[一-鿿]+")
TOPN = 4          # 一个议题最多取几个词当签名（**封顶 = 受控**，不是"共享即同族"）
FURNITURE_PCT = 0.02


def grams(s, lo=2, hi=4):
    out = set()
    for m in CJK.finditer(s or ""):
        w = m.group(0)
        for n in range(lo, hi + 1):
            for i in range(len(w) - n + 1):
                out.add(w[i:i + n])
    return out


def load():
    """→ rows: (topic_id, label, title, day)  day = 'MM-DD'（按 fetched_at）"""
    rows = corpus.psql_rows("""
        SELECT t.id, coalesce(t.label,''), coalesce(i.title,''),
               to_char(i.fetched_at, 'MM-DD')
        FROM feed_topics t
        JOIN feed_item_topics it ON it.topic_id = t.id
        JOIN feed_items i ON i.id = it.item_id""")
    out = []
    for r in rows:
        if len(r) >= 4 and r[0]:
            out.append((int(r[0]), r[1], r[2], r[3]))
    return out


def build_sig(titles, furniture, share=0.5, minlen=3):
    """签名 = 覆盖 ≥ share 比例成员的词（去家具），按"覆盖数↓、长度↓"取前 TOPN 个。

    ⚠️ `share` 是**这根轴上唯一的旋钮**：调大 ⇒ 签名更"共有"（更稳）但更没区分力；
    调小 ⇒ 更容易撞车。第五节把它扫一遍 —— **两端都量过了，中间有没有甜点是可判的**。
    """
    if not titles:
        return frozenset()
    cnt = Counter()
    for tg in titles:
        for g in grams(tg) - furniture:
            cnt[g] += 1
    need = max(2, len(titles) * share)
    keep = [g for g, c in cnt.items() if c >= need and len(g) >= minlen]
    keep.sort(key=lambda g: (-cnt[g], -len(g)))
    out = []
    for g in keep:
        if not any(g in k for k in out):       # 去掉被更长片段包含的
            out.append(g)
    return frozenset(out[:TOPN])


def main():
    topn = 12
    if "--top" in sys.argv:
        topn = int(sys.argv[sys.argv.index("--top") + 1])
    rows = load()

    # 家具词按**末日全量**定一次（回放里保持一致，否则每天的家具表都变，曲线没法读）
    df = Counter()
    for _, _, title, _ in rows:
        for g in grams(title):
            df[g] += 1
    total_titles = len(rows)
    furniture = {g for g, c in df.items() if c > total_titles * FURNITURE_PCT}

    by_topic = defaultdict(list)          # tid -> [(day, label, title)]
    for tid, lab, title, day in rows:
        by_topic[tid].append((day, lab, title))
    days = sorted({d for _, _, _, d in rows if d})

    print("=" * 76)
    print(f"议题 {len(by_topic)} 个 · 标题 {total_titles} 条 · 语料只有 {len(days)} 天"
          f"（{days[0]} ~ {days[-1]}）⇒ **曲线只有 6 个点，看形状、不看斜率**")
    print(f"家具词（全局标题词频 > {FURNITURE_PCT:.0%}）：{len(furniture)} 个"
          f"　例：{'、'.join(sorted(furniture)[:8])}")
    print("=" * 76)

    # ── 一、逐日回放 ─────────────────────────────────────────────────────
    print("\n一、**逐日回放**（每天只用那天的成员重建签名）")
    print(f"  {'日期':<7}{'议题数':>7}{'入口数':>7}{'最大入口':>9}{'无签名的议题':>14}"
          f"{'新增条目':>9}{'新入口':>7}{'新入口/条目':>12}")
    prev_entries, prev_topics, prev_items = None, 0, 0
    curve = []
    for D in days:
        # 截至 D：议题的成员只取 <= D 的那些（议题在这一天"长成"什么样）
        sigs = {}
        items_n = 0
        for tid, mem in by_topic.items():
            upto = [t for d, _, t in mem if d <= D]
            if not upto:
                continue
            items_n += len(upto)
            # ⚠️ 签名只在 ≥2 条时才有意义（单条议题的"过半"就是它自己 ⇒ 退化成标题里的词）
            if len(upto) >= 2:
                sigs[tid] = build_sig(upto, furniture)
        groups = defaultdict(list)
        for tid, s in sigs.items():
            if s:
                groups[s].append(tid)
        entries = {s for s in groups}
        no_sig = len(sigs) - sum(len(v) for v in groups.values())
        biggest = max((len(v) for v in groups.values()), default=0)
        new_e = 0 if prev_entries is None else len(entries - prev_entries)
        new_i = items_n - prev_items
        ratio = f"{new_e/new_i:.3f}" if new_i > 0 else "—"
        print(f"  {D:<7}{len(sigs):>7}{len(entries):>7}{biggest:>9}{no_sig:>14}"
              f"{new_i:>9}{new_e:>7}{ratio:>12}")
        curve.append((D, len(sigs), len(entries), items_n))
        prev_entries, prev_topics, prev_items = entries, len(sigs), items_n
    print("  （**议题层的对照**：新议题/新条目 = **0.577**）")
    print("  （入口数只用 ≥2 条成员的议题建 —— 单条的「过半」就是它自己，会退化成标题词）")

    final = days[-1]
    keep = {}
    for tid, mem in by_topic.items():
        upto = [t for d, _, t in mem if d <= final]
        if len(upto) >= 2:
            keep[tid] = build_sig(upto, furniture)
    g_final = defaultdict(list)
    for tid, s in keep.items():
        if s:
            g_final[s].append(tid)
    print(f"\n  末日：≥2 条的议题 {len(keep)} 个 → **入口 {len(g_final)} 个**"
          f"（压缩 **{len(keep)/max(len(g_final),1):.1f} : 1**）· "
          f"最大入口挂 {max((len(v) for v in g_final.values()), default=0)} 个议题")

    # ── 二、签名的稳定性 ─────────────────────────────────────────────────
    print("\n二、**签名稳不稳**：同一个议题，回放到中途那天的签名与末日一致吗？")
    print(f"  {'截止到':<7}{'可比议题':>9}{'签名一致':>9}{'一致率':>8}")
    mid = days[len(days) // 2]
    for D in days:
        same = tot = 0
        for tid, mem in by_topic.items():
            u_d = [t for d, _, t in mem if d <= D]
            u_f = [t for d, _, t in mem if d <= final]
            if len(u_d) < 2 or len(u_f) < 2:
                continue
            tot += 1
            if build_sig(u_d, furniture) == build_sig(u_f, furniture):
                same += 1
        if tot:
            print(f"  {D:<7}{tot:>9}{same:>9}{same/tot*100:>7.0f}%")
    print("  ⚠️ 中途那列天然偏高（成员还没到齐、签名还没被后来的报道改写）——")
    print("     要看的是**越靠前越低**：低多少 = 后来又来的报道把签名改写掉多少")

    # ── 三、单条议题的归宿 ───────────────────────────────────────────────
    print("\n三、**单条议题（81%）的归宿**：它们能不能落进已有入口？")
    tails = {s for s in g_final}
    words = Counter()
    for s in tails:
        for g in s:
            words[g] += 1
    hit = miss = 0
    for tid, mem in by_topic.items():
        upto = [t for d, _, t in mem if d <= final]
        if len(upto) != 1:
            continue
        tg = grams(upto[0])
        if any(g in tg for g in words):
            hit += 1
        else:
            miss += 1
    print(f"   单条议题 {hit+miss} 个：标题含某个**入口签名词**的 **{hit}**"
          f"（{hit/max(hit+miss,1)*100:.0f}%）· 落不进去的 {miss}")
    print("   ⇒ 落得进去 ⇒ 它只是「这个入口的第 N 条」；落不进 ⇒ 它自成一个新入口")

    # ── 四、末日样本（人眼核）─────────────────────────────────────────────
    print(f"\n四、**末日样本**：最大的 {topn} 个入口，各挂着哪些议题")
    for s, tids in sorted(g_final.items(), key=lambda x: -len(x[1]))[:topn]:
        print(f"\n   入口 {sorted(s)}　← {len(tids)} 个议题")
        for tid in tids[:8]:
            lab = [l for d, l, _ in by_topic[tid] if l]
            print(f"      · {(lab[0] if lab else '?')[:34]}")
        if len(tids) > 8:
            print(f"      … 还有 {len(tids)-8} 个")

    # ── 五、⭐ 把这根轴扫一遍：**压缩 vs 撞车** 的连续谱 ──────────────────
    #
    # 为什么必须有这一节：前四节只量了 `share=0.5` 那**一个点**，而结论"不压缩"完全
    # 可能是**阈值选得不好**，不是方法不行。今晨量过另一头：共享词**单链**（≈ share→0
    # 且传递闭包）能压到 47 入口，但最大入口 **1870/1923**（全库一坨）。
    # ⇒ **两端都量过，中间有没有甜点是个可以判的问题** —— 扫一遍，别只守着一个点。
    print("\n" + "=" * 76)
    print("五、⭐ **扫一遍**：签名松紧 vs 压缩比 / 撞车 / 抽不出词 的连续谱")
    print("   `share` = 一个词要覆盖多少比例的成员才算签名词（越小越松）")
    print(f"   {'share':>6}{'入口数':>8}{'压缩比':>9}{'最大入口':>9}{'唯一签名':>9}"
          f"{'抽不出词':>9}")
    base_tids = [tid for tid, mem in by_topic.items()
                 if len([t for d, _, t in mem if d <= final]) >= 2]
    for share in (1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1):
        sigs = {}
        for tid in base_tids:
            upto = [t for d, _, t in by_topic[tid] if d <= final]
            sigs[tid] = build_sig(upto, furniture, share=share)
        g = defaultdict(list)
        for tid, s in sigs.items():
            if s:
                g[s].append(tid)
        n_entry = len(g)
        comp = len(base_tids) / max(n_entry, 1)
        biggest = max((len(v) for v in g.values()), default=0)
        uniq = sum(1 for v in g.values() if len(v) == 1)
        empty = len(base_tids) - sum(len(v) for v in g.values())
        print(f"   {share:>6.1f}{n_entry:>8}{comp:>8.2f}:1{biggest:>9}"
              f"{uniq/max(n_entry,1)*100:>8.0f}%{empty:>9}")
    print("   ⚠️ **两端都量过了**：`share=0.5` → 压缩 **1.08:1**（几乎不压，等于给议题换个名字）；")
    print("      而**共享词单链**（今晨）→ 47 入口（压缩 ~41:1）但**最大入口 1870/1923**（全库一坨）。")
    print("      ⇒ 要看的是**中间有没有一个「既压得动、最大入口又还像个组」的点**；")
    print("        若最大入口随 share 下降**一路暴涨**（没有平台期），这条轴上**没有甜点**。")

    # ── 六、⚠️ 自查：**是不是我自己的家具阈值把跨议题的词滤掉了？** ──────
    #
    # 第五节"最大入口从头到尾都是 2"太整齐了，值得怀疑。查一下：
    # 家具阈值取 2% 时，被滤掉的词里有 **「亚运」「中秋」「习近平」** —— 而它们**正是**
    # 能让两个议题撞进同一个入口的那类词。**把跨议题的键先滤掉，当然压不动。**
    # ⇒ 这是"集合不同 = 静默高估/低估"那一族：**一个负结论完全可能是仪器造的**。
    print("\n" + "=" * 76)
    print("六、⚠️ **自查**：那个「几乎不压缩」是不是**我自己的家具阈值**造成的？")
    print("   （2% 的阈值把「亚运」「中秋」「习近平」都当成了家具 —— 而它们正是**跨议题的键**）")
    print(f"   {'家具阈值':>9}{'share=0.5 入口':>15}{'压缩比':>9}{'最大入口':>9}"
          f"{'share=0.4 入口':>15}{'最大入口':>9}")
    for pct_label, pct in (("2%", 0.02), ("10%", 0.10), ("30%", 0.30), ("关", 9.9)):
        furn = {g for g, c in df.items() if c > total_titles * pct}
        row = f"   {pct_label:>9}"
        for share in (0.5, 0.4):
            sigs = {}
            for tid in base_tids:
                upto = [t for d, _, t in by_topic[tid] if d <= final]
                sigs[tid] = build_sig(upto, furn, share=share)
            g = defaultdict(list)
            for tid, s in sigs.items():
                if s:
                    g[s].append(tid)
            big = max((len(v) for v in g.values()), default=0)
            comp = len(base_tids) / max(len(g), 1)
            row += f"{len(g):>15}{comp:>8.2f}:1" if share == 0.5 else f"{len(g):>15}"
            row += f"{big:>9}"
        print(row)
    print("   ⚠️ 若「关掉家具过滤」之后压缩比**明显变好**，那么第五节的负结论**是仪器造的** ——")
    print("      正确的话是：**字面键要能用，第一件事是分清「家具」与「实体」**，")
    print("      而**频率分不开这两者**（亚运出现在 8.3% 的标题里，比站点名还高）。")

    # ── 七、⭐ **反转过来**：先找跨议题的词，再拿它挂议题 ────────────────────
    #
    # 第五、六节合起来说明：**"从议题自己的标题里抽 top-k 词"抽的是这个议题的指纹**，
    # 不是跨议题的键 —— 无论 share 怎么调、家具滤不滤，压缩比都在 1.2~1.6:1。
    # （两个我自己的设计选择在系统性选"特异的词"：① 家具阈值把高频实体滤掉
    #   ② `-len` 破平让 4 字特异词压过 2 字共有词。）
    # ⇒ 那就**反过来**：入口**不由议题产生**，而是**先在所有标题上找出反复出现的词**
    #   （覆盖 5~60 个议题的那一档），再让每个议题**挂到**覆盖它的前 K 个上。
    #   这正是今晨"共享词"那节量到的形状（前 25 个共享词覆盖 826 议题）。
    print("\n" + "=" * 76)
    print("七、⭐ **反转**：入口先于议题 —— 先找跨议题的词，再让议题挂上去")
    flat = {}
    for tid in base_tids:
        upto = [t for d, _, t in by_topic[tid] if d <= final]
        flat[tid] = grams(" ".join(upto)) - furniture
    # 入口词 = 覆盖多少个**议题**（不是条目），落在 5~60 之间
    cover = Counter()
    for tid, tg in flat.items():
        for g in tg:
            if len(g) >= 3:
                cover[g] += 1
    print(f"   {'入口词门槛':>10}{'入口数':>8}{'被挂上的议题':>12}{'覆盖率':>8}"
          f"{'最大入口':>9}{'平均每议题挂':>12}")
    for lo, hi in ((5, 60), (5, 200), (3, 60), (2, 60), (5, 999)):
        words = {g for g, c in cover.items() if lo <= c <= hi}
        # 每个议题挂到"覆盖它的入口词"里最长的前 K 个
        K = 3
        hang = defaultdict(list)
        covered = 0
        for tid, tg in flat.items():
            hitw = sorted((g for g in tg if g in words), key=len, reverse=True)
            picked = []
            for g in hitw:
                if not any(g in k for k in picked):
                    picked.append(g)
                if len(picked) >= K:
                    break
            if picked:
                covered += 1
            for g in picked:
                hang[g].append(tid)
        big = max((len(v) for v in hang.values()), default=0)
        avg = sum(len(v) for v in hang.values()) / max(len(flat), 1)
        print(f"   {f'{lo}~{hi}':>10}{len(hang):>8}{covered:>12}"
              f"{covered/max(len(flat),1)*100:>7.0f}%{big:>9}{avg:>12.2f}")
    print("   ⇒ 要看的是：**入口数远小于议题数（压得动）**，而**最大入口没炸**（没串）。")
    print("     ⚠️ 若「最大入口」随门槛放松一路涨到几百，那就是今晨那个链式的老毛病；")
    print("        若入口数一放松就逼近议题数，那说明**词这一层根本没那个结构**。")

    # ── 八、反转版的**逐日曲线**（用户真正问的那句：入口数稳不稳）────────────
    print("\n八、⭐ **反转版的逐日曲线**（门槛取 5~60 —— 第七节里唯一「压得动又不串」的那档）")
    print(f"   {'日期':<7}{'议题数':>7}{'入口数':>7}{'最大入口':>9}{'覆盖议题':>9}"
          f"{'新增入口':>9}{'新增议题':>9}{'入口/议题':>10}")
    prev_e, prev_t = None, 0
    for D in days:
        flat_D = {}
        for tid, mem in by_topic.items():
            upto = [t for d, _, t in mem if d <= D]
            if len(upto) >= 2:
                flat_D[tid] = grams(" ".join(upto)) - furniture
        cov = Counter()
        for tid, tg in flat_D.items():
            for g in tg:
                if len(g) >= 3:
                    cov[g] += 1
        words = {g for g, c in cov.items() if 5 <= c <= 60}
        hang = defaultdict(list)
        covered = 0
        for tid, tg in flat_D.items():
            hitw = [g for g in tg if g in words]
            if hitw:
                covered += 1
                for g in hitw:
                    hang[g].append(tid)
        ent = set(hang)
        ne = 0 if prev_e is None else len(ent - prev_e)
        nt = len(flat_D) - prev_t
        r = f"{ne/nt:.3f}" if nt > 0 else "—"
        print(f"   {D:<7}{len(flat_D):>7}{len(ent):>7}"
              f"{max((len(v) for v in hang.values()), default=0):>9}{covered:>9}"
              f"{ne:>9}{nt:>9}{r:>10}")
        prev_e, prev_t = ent, len(flat_D)
    print("   ⚠️ 只有 6 个点，而且每天都是「累积重建」（词的门槛是跨议题计数，天然滞后）")
    print("      ⇒ **看形状**：入口数的增长是不是**明显慢于议题数**；别读斜率。")
    print("   ⚠️⚠️ **本节与第七节的入口数不可直接比**（我差点又栽在「集合不同」上）：")
    print("      第七节是**受控版**（每个议题只挂最长的 top-3 个入口词）⇒ 末日 57 个入口；")
    print("      本节**没封顶**（挂上所有命中的入口词）⇒ 末日 112 个。")
    print("      按「必须受控」这条规矩，**该看的是第七节那 57**；本节只用来看**曲线的形状**。")

    print("\n" + "=" * 76)
    print("读法：压缩与撞车**必须一起看** —— 只看压缩会把「全库一坨」当成成功，")
    print("      只看撞车会把「几乎没压」当成安全。**两个都不是结论。**")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
