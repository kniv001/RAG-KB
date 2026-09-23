# -*- coding: utf-8 -*-
"""
**读 decode-split-probe 留下的原文，把思考拆到"句"这一层** —— 时间与内容对上。

为什么要单独一步（而不是让探针直接算）：
    判"这句是不是在重复前面说过的"需要**读原文、看样例**才能定阈值 ——
    本项目在 `2026-09-21-少想这条线收口` 里吃过一次亏：五条机械指标**全都指错方向**，
    因为它们只抓"逐字"，而被抓的对象是**同一句话换措辞说十几遍**（shingle 一碰就错开）。
    所以这里的判据是**字符二元组 Jaccard**（对换字、增删字稳健），
    并且**必须把命中的句对打出来供人核对** —— 数字和样例一起给，不信数字。

两件事：
    ① **时间映射**：帧 → (累计字数, 时刻)，于是任意字符位置都能查到"它是什么时候写下的"
    ② **句级去重**：每句与**更早的每一句**比，最高的那个过阈值 ⇒ 记"重复"，
       报重复的**字数占比**与**折合秒数**

用法：python tools/decode-split-read.py tools/_runs/decode-split/<时间戳>
"""
import glob
import io
import json
import os
import re
import sys

THRESH = 0.55      # 二元组 Jaccard 阈值：**默认值本身就是个判断，脚本会把样例打出来**
MIN_LEN = 10       # 太短的句子（"所以"、"但是"）比出来的相似度没有意义


def bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def jac(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, len(A | B))


def sentences(text):
    """切句：以句末标点与换行为界，**保留标点**，并记下每句的字符区间。"""
    out, start = [], 0
    for m in re.finditer(r"[。！？；\n]+", text):
        end = m.end()
        seg = text[start:end]
        if seg.strip():
            out.append((start, end, seg))
        start = end
    if text[start:].strip():
        out.append((start, len(text), text[start:]))
    return out


def time_at(frames, offset):
    """字符 offset 是什么时候写下的：帧给的是 (帧末累计字数, 时刻)，之间按字数线性内插。"""
    cum = 0
    prev_t, prev_c = None, 0
    for f in frames:
        t, c = f["t"] / 1000.0, cum + f["len"]
        if c >= offset:
            if prev_t is None or c == prev_c:
                return t
            frac = (offset - prev_c) / max(1, c - prev_c)
            return prev_t + frac * (t - prev_t)
        cum, prev_t, prev_c = c, t, c
    return frames[-1]["t"] / 1000.0 if frames else float("nan")


def kind_of(seg):
    """大致归类 —— **只是给读的人一个分组，不是判据**。归类错了不影响①的秒数。"""
    s = seg
    if re.search(r"理解(用户|一下)?(的)?问题|用户的问题是|问题是|需要先理解", s):
        return "复述问题"
    if re.search(r"检查(知识库|资料)|参考资料|\[1\]|主题概览|有没有(相关)?资料|逐(条|个)", s):
        return "查资料"
    if re.search(r"属于【|判(为|定)|知识库(中)?(确实)?没有|有相关资料|【[甲乙丙]】", s):
        return "判类型"
    if re.search(r"根据(系统)?要求|我需要(这样)?(回答|做)|要求我|按(照)?(这个)?(结构|格式)|回答(应该|需要)", s):
        return "复述契约"
    return "起草/其它"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(glob.glob(os.path.join(d, "*-frames.json")))
    if not files:
        raise SystemExit(f"{d} 里没有 *-frames.json —— 先跑 decode-split-probe.mjs")
    agg = []

    for fp in files:
        sec = 0.0                 # 没有重复句时也要有值（汇总那步要用）
        meta = json.load(io.open(fp, encoding="utf-8"))
        think = io.open(fp.replace("-frames.json", "-thinking.txt"), encoding="utf-8").read()
        answer = io.open(fp.replace("-frames.json", "-answer.txt"), encoding="utf-8").read()
        frames = meta["frames"]
        ai = next((i for i, f in enumerate(frames) if f["ch"] == "ans"), None)
        if ai is None or not think:
            print(f"\n{fp}：缺一路通道，跳过")
            continue
        # **只取"转正文之前"的思考帧** —— 补推的那一帧的时刻在全部正文之后，
        # 拿它做时间映射会把最后一批字的时刻算到 20 秒之后。
        tf = frames[:ai]
        first_think_t = tf[0]["t"] / 1000.0
        first_ans_t = frames[ai]["t"] / 1000.0
        span = first_ans_t - first_think_t

        print("\n" + "=" * 78)
        print(f"题：{meta['q'][:60]}")
        print(f"思考 {len(think)} 字（{len(tf)} 帧 / 每帧 60 字）· 正文 {len(answer)} 字"
              f"· 思考窗口 {span:.1f}s ⇒ {len(think)/max(1e-9,span):.0f} 字/秒")

        # ── ① 句级去重 ─────────────────────────────────────────────
        sents = [(a, b, s) for a, b, s in sentences(think) if len(s.strip()) >= MIN_LEN]
        reps, kept = [], []
        for a, b, s in sents:
            best, best_j = None, 0.0
            for (pa, pb, ps) in kept:
                j = jac(s, ps)
                if j > best_j:
                    best_j, best = j, ps
            if best is not None and best_j >= THRESH:
                reps.append((a, b, s, best, best_j))
            else:
                kept.append((a, b, s))
        rc = sum(b - a for a, b, _, _, _ in reps)
        print(f"\n① 句级去重（二元组 Jaccard ≥ {THRESH}，句子 ≥ {MIN_LEN} 字才参评）")
        print(f"   句 {len(sents)}　其中**重复** {len(reps)} 句 / {rc} 字"
              f"（占思考 {100*rc/max(1,len(think)):.0f}%）")
        if reps:
            t_first = time_at(tf, reps[0][0])
            t_last = time_at(tf, reps[-1][1])
            # 折合秒数：逐句按它在时间轴上的区间算
            sec = 0.0
            for a, b, _, _, _ in reps:
                sec += time_at(tf, b) - time_at(tf, a)
            print(f"   折合 **{sec:.1f}s**（占思考窗口 {100*sec/max(1e-9,span):.0f}%）"
                  f"　首句重复出现在 {t_first-first_think_t:.1f}s，末句 {t_last-first_think_t:.1f}s")
            print("   命中最高的三对（供核对阈值是否合适）：")
            for a, b, s, best, j in sorted(reps, key=lambda x: -x[4])[:3]:
                print(f"     [{j:.2f}] 后：{s.strip()[:64]}")
                print(f"            前：{best.strip()[:64]}")

        # ── ② 按内容分段（归类是给人看的，秒数才是数）─────────────────
        print("\n② 思考按内容分段（**归类是粗的、只看个大概**；字号是这一类的字数）")
        buckets = {}
        for a, b, s in sents:
            k = kind_of(s)
            dt = time_at(tf, b) - time_at(tf, a)
            g = buckets.setdefault(k, [0, 0.0, 0])
            g[0] += 1
            g[1] += dt
            g[2] += len(s)          # 2026-09-22 修：原来只在建桶时存了首句长度，
                                    # 于是打出"68 句 13 字"这种自相矛盾的读数
        for k, (n, dt, ch) in sorted(buckets.items(), key=lambda x: -x[1][1]):
            print(f"   {k:<8}{n:>4} 句　{ch:>5} 字　{dt:>6.1f}s"
                  f"　({100*dt/max(1e-9,span):>3.0f}% 的思考窗口)")
        print("   ⚠ 归类靠的是关键词正则，**不是判据** —— 分段的意义只在于"
              "「复述契约/复述问题」这类**纯开销**有多重；其余一律并进起草。")

        print("\n③ 前后半段对照（思考是不是越写越慢）")
        half = len(think) / 2
        t_half = time_at(tf, half)
        r1 = half / max(1e-9, t_half - first_think_t)
        r2 = (len(think) - half) / max(1e-9, first_ans_t - t_half)
        print(f"   前半 {half:.0f} 字 {r1:.0f} 字/秒　后半 {len(think)-half:.0f} 字 {r2:.0f} 字/秒"
              f"　⇒ {'衰减' if r2 < r1*0.95 else '基本恒定'}")

        agg.append({"q": meta["q"], "think": len(think), "ans": len(answer),
                    "span": span, "rep_chars": rc, "rep_sec": sec if reps else 0.0,
                    "rep_n": len(reps), "n": len(sents),
                    "half_ratio": r2 / max(1e-9, r1)})

    # ── 跨题汇总 ───────────────────────────────────────────────────────
    if len(agg) < 2:
        return
    print("\n" + "=" * 78)
    print(f"跨题汇总（n={len(agg)}）")
    med = lambda v: sorted(v)[len(v) // 2]
    print(f"  思考字数 中位 {med([a['think'] for a in agg])}"
          f"（{min(a['think'] for a in agg)}~{max(a['think'] for a in agg)}）"
          f"　正文字数 中位 {med([a['ans'] for a in agg])}"
          f"（{min(a['ans'] for a in agg)}~{max(a['ans'] for a in agg)}）")
    print(f"  思考窗口 中位 {med([a['span'] for a in agg]):.1f}s"
          f"（{min(a['span'] for a in agg):.1f}~{max(a['span'] for a in agg):.1f}）")
    print(f"  **重复句占比（字数）中位 {100*med([a['rep_chars']/max(1,a['think']) for a in agg]):.0f}%**"
          f"（{100*min(a['rep_chars']/max(1,a['think']) for a in agg):.0f}%"
          f"~{100*max(a['rep_chars']/max(1,a['think']) for a in agg):.0f}%）"
          f"　折合秒数中位 {med([a['rep_sec'] for a in agg]):.1f}s")
    print(f"  后半/前半速率比 中位 {med([a['half_ratio'] for a in agg]):.2f}"
          f"（<1 = 越写越慢）")
    print(f"  最极端那题：思考 {max(agg, key=lambda a: a['think'])['think']} 字 / "
          f"正文 {min(agg, key=lambda a: a['think'])['ans']} 字 —— "
          f"重复占 {100*max(agg, key=lambda a: a['rep_chars']/max(1,a['think']))['rep_chars']/max(agg, key=lambda a: a['rep_chars']/max(1,a['think']))['think']:.0f}%")
    print("  ⚠ 阈值 0.55 是**判断**，不是刻度 —— 上面每题都打了命中最高的三对，"
          "觉得太松/太紧就调 THRESH 重跑（纯读文件，不重跑模型）。")


if __name__ == "__main__":
    main()
