# -*- coding: utf-8 -*-
"""
**「多个主语组成的复合键能当议题的唯一性判别键吗」** —— 用户 2026-09-29 提问。

提议：把「北京-亚运」这样的**多主语组合**当成一个议题的**唯一性判别键**。
（区别于"单个主语当检索键"那一问：那一问的答案是"一个键底下挂着 14~134 件事"。）

一个键要能当**判别键**，要同时满足两条，而它们是**反向拉扯**的：
  · **区分力**：不同的事，键不同     —— 词多了自然更强（但那是白给的）
  · **稳定性**：同一件事的不同报道，键**相同** —— 这一条才是真考验

所以本探针**从两头量**：

  一、**稳定性（同议题内）**：一个议题的多条报道，标题里**共同**的词有几个？
      —— 共同词少 ⇒ 任何"从内容抽词"的键**在同一条议题内部就已经不唯一**了
  二、**区分力（跨议题）**：拿"成员标题的公共词组合"当签名，有多少议题签名**唯一**？
      有多少**撞车**（两件不同的事签名一样）？
  三、**变体碎片**：同一个实体的不同字面（亚运 / 亚运会 / 爱知·名古屋亚运会）
      会挂成**不同的键** —— 这是任何字面键的固有代价，量出来
  四、**链式**：与"共享词单链并簇"是同一个失败模式（今晨量过：最短词长 2 → 47 入口/最大 1870）

⚠️ 本探针**只用标题与议题标签，不需要模型**（与今晨那两个探针同一套 n-gram 代理）。
用法：python tools/topic-composite-key-probe.py
"""
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ruler import corpus  # noqa: E402

STOP = set("中国 我国 全国 中方 国家 记者 报道 举行 举办 启动 发布 召开 推进 加强 提升 实现 "
           "表示 指出 强调 介绍 今年 昨天 今天 明天 近日 日前 目前 以来 同时 以及 记者会 "
           "发布会 工作 有关 方面 问题 情况 活动 发展 建设 习近平 李强 消息 综合 视频 图片 "
           "中新网 客户端 通讯 本报 报道 记者 编辑 责编".split())
CJK = re.compile(r"[一-鿿]+")


def grams(s, lo=2, hi=4):
    """2~4 字的连续汉字片段 —— 本项目一直用的"主语"代理（不是分词器）。"""
    out = set()
    for m in CJK.finditer(s or ""):
        w = m.group(0)
        for n in range(lo, hi + 1):
            for i in range(len(w) - n + 1):
                g = w[i:i + n]
                if g not in STOP:
                    out.add(g)
    return out


def load():
    rows = corpus.psql_rows("""
        SELECT t.id, coalesce(t.label,''), i.title
        FROM feed_topics t
        JOIN feed_item_topics it ON it.topic_id = t.id
        JOIN feed_items i ON i.id = it.item_id
        WHERE i.title IS NOT NULL""")
    topics = defaultdict(list)
    for r in rows:
        if len(r) >= 3 and r[0]:
            topics[int(r[0])].append((r[1] or "", r[2] or ""))
    return topics


def main():
    topics = load()
    multi = {t: v for t, v in topics.items() if len(v) >= 2}
    print("=" * 74)
    print(f"议题 {len(topics)} 个（其中 ≥2 条报道的 {len(multi)} 个）")
    print("=" * 74)

    # ── 0、**先按分布定"家具词"**（不手写词表）─────────────────────────
    # 第一版把"公共词"定义成"出现在该议题**所有**成员标题里"，而且用一份**手写**停用词表。
    # 两个毛病①"所有成员共有"太严（34 条的议题几乎不可能）②手写表盖不住**站点名** ——
    # 实测最大撞车那 25 个议题的签名是 ['界面新','界面新闻','面新闻']，那是**家具不是主语**。
    # ⇒ 改成：**全局标题词频超过 2% 的 n-gram 一律当家具**（分布说了算，与手写表无关）。
    allt = [x[1] for v in topics.values() for x in v]
    df = Counter()
    for title in allt:
        for g in grams(title):
            df[g] += 1
    FURNITURE = {g for g, c in df.items() if c > len(allt) * 0.02}

    def sig_of(v, share=0.5):
        """签名 = 出现在该议题**≥share 比例成员**标题里的词（去掉家具词），取最长的 4 个。"""
        gs = [grams(title) - FURNITURE for _, title in v]
        cnt = Counter()
        for s in gs:
            for g in s:
                cnt[g] += 1
        keep = [g for g, c in cnt.items() if c >= max(2, len(v) * share) and len(g) >= 3]
        # 去掉被更长片段包含的短片段（「世界技能」与「世界技能大」是同一个东西）
        keep.sort(key=len, reverse=True)
        out = []
        for g in keep:
            if not any(g in k for k in out):
                out.append(g)
        return frozenset(out[:4])

    print(f"   （家具词：全局标题词频 > 2% 的 n-gram，共 {len(FURNITURE)} 个；"
          f"例：{'、'.join(sorted(FURNITURE)[:6])}）")

    # ── 一、稳定性：一个议题的多条报道，"过半成员共有"的词有几个 ─────────
    print("\n一、**稳定性**：同一议题内，**过半成员标题共有**的词有几个？")
    hist = Counter()
    examples = []
    for t, v in multi.items():
        s = sig_of(v)
        hist[len(s)] += 1
        if not s and len(examples) < 8:
            examples.append((v[0][0], [x[1][:42] for x in v[:4]]))
    mx = max(hist.values()) if hist else 1
    for k in sorted(hist)[:10]:
        print(f"   签名词 {k:>2} 个：{hist[k]:>4} 个议题  "
              + "█" * max(0, min(40, int(hist[k] * 40 / mx))))
    zero = hist.get(0, 0)
    print(f"   ⇒ **一个签名词都抽不出的议题：{zero}/{len(multi)} = "
          f"{zero/max(len(multi),1)*100:.0f}%**")
    if examples:
        print("\n   样本（这几件「多条报道的事」，过半成员连一个公共词都没有）：")
        for lab, ts in examples:
            print(f"     [{lab[:18]}]")
            for x in ts:
                print(f"        · {x}")

    # ── 二、区分力：签名撞车 ──────────────────────────────────────────────
    print("\n二、**区分力**：拿这个签名当唯一性判别键，撞不撞车？")
    sigs = defaultdict(list)
    for t, v in multi.items():
        sigs[sig_of(v)].append(t)
    uniq = sum(1 for s, ts in sigs.items() if len(ts) == 1 and s)
    empty = len(sigs.get(frozenset(), []))
    clash = max((len(ts) for s, ts in sigs.items() if s), default=0)
    print(f"   签名唯一的议题：{uniq}/{len(multi)} = {uniq/max(len(multi),1)*100:.0f}%")
    print(f"   签名**为空**（抽不出词）的议题：{empty}")
    print(f"   最大撞车：一个签名挂了 {clash} 个议题")
    for s, ts in sorted(sigs.items(), key=lambda x: -len(x[1]))[:5]:
        if not s:
            continue
        print(f"     签名 {sorted(s)} 挂了 {len(ts)} 个议题：" +
              " · ".join((topics[t][0][0] or "?")[:12] for t in ts[:5]))
    print("   ⇒ 撞车就是**串**（两件不同的事被判成同一件）；空签名就是**漏**（判不出来）")
    print("     —— **同一个键不可能两头都好**，跟阈值一样要选一个位置")

    # ── 三、变体碎片：同一实体的不同字面 = 不同的键 ──────────────────────
    print("\n三、**变体碎片**：同一个实体的不同字面，会挂成不同的键")
    for group in (["亚运", "亚运会", "爱知", "名古屋"], ["习近平", "习主席", "国家主席"],
                  ["中秋", "中秋节", "月饼"]):
        print("   " + "  ".join(
            f"「{w}」{sum(1 for v in topics.values() if any(w in x[1] for x in v))} 议题"
            for w in group))
    print("   ⇒ 字面键**天生**分不清「亚运」与「亚运会」—— 除非上面再加一层归一")
    print("     （今晨量过：亚运 59 · 运会 52 · 亚运会 51，三个键指的是同一件事）")

    # ── 四、链式：与「共享词单链并簇」同一个失败模式 ──────────────────────
    print("\n四、**链式**（与今晨「共享词单链并簇」同一个失败模式）")
    print("   两个议题只要**共享一个词**就算连上，传递闭包会把不相干的事连成一片：")
    for w in ("中国", "北京", "美国", "亚运"):
        n = sum(1 for v in topics.values() if any(w in x[1] for x in v))
        print(f"     「{w}」把它底下 {n} 个议题**两两连通**")
    print("   ⇒ 今晨实测：最短词长 2 的单链 → **47 个入口、最大入口 1870/1923**（全库一坨）")
    print("     ⇒ 复合键若也走「共享即同族」，**同一个地方再炸一次**；")
    print("       要能用，必须**受控**（一个议题挂有限个键），不能传递闭包")

    print("\n" + "=" * 74)
    print("读法：第一节（稳定性）与第二节（区分力）**必须一起看** ——")
    print("  公共词=0 的议题占多少 ⇒ 决定了这个键**天生就漏掉多少**；")
    print("  撞车多大 ⇒ 决定了它**会串多少**。两个都不能只看一个。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
