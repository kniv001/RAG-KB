# -*- coding: utf-8 -*-
"""
**思考的结构** —— 沿时间轴看，那些字到底在干什么。

## 为什么不能用分类器

`decode-split-read.py` 里那版按关键词分「复述问题/查资料/判类型/复述契约/起草」，
**89% 落进"其它"** —— 因为它假设思考是一段段做完的事，而实际是**一条连续的话**。
（这正是 `2026-09-21-少想这条线收口` 里那五条指标的同类错误：形状想当然。）

## 换成三个**能机械判定**的量

判据都只用**已经给过它的文本**做比对 —— 复述的判据天然是"这段字有多少能在别处找到"：

1. **复述问句**：该句的字符二元组有多少落在**用户问句**里（≥0.7 记复述）
2. **复述契约**：……落在**系统提示词**里（≥0.8）。系统提示从 Java 源码的文本块机械抠出来
3. **复述资料**：**能对上实际注入的那几段切块里的某一句话**（整句 Jaccard ≥0.6）。
   切块由 `done` 的 sources 反查数据库 ⚠️ 第一版用的是"覆盖率≥0.8"，**假阳性 52%**，见下
4. **重复自己**：与前文某个**更早的思考句**的二元组 Jaccard ≥ 0.6

前三条都是**纯开销**的候选：那些字**已经在提示词里**，再写一遍不产生新信息。
第四条是"换措辞说十几遍"。

## 再加一个真正关乎优化的量：**正文的内容什么时候想够了**

把**最终正文的二元组集合**当目标，看累计想过多少 ⇒ 一条单调上升的曲线，
取它跨过 50% / 80% / 95% 的时刻。跨过 80% 之后剩下的时间，就是"早停类改动"的**上限**。

⚠️ 这里换过一版判据，两个毛病都是实测出来的：句级匹配 ① **假阳性**（思考复述问句、
而正文首句也正好是问句 ⇒ 0.0s 就"命中"，令牌桶那题就是）② **长答案根本不触发**
（759 字的正文，草稿与最终措辞差得够远，Jaccard 上不到 0.6 ⇒ 整题"没找到"，
而那明显不是事实）。曲线版两个毛病都没有。

⚠️ 曲线是"**想过**"不是"**想好了**" —— 跨过 80% 也不代表剩下的不用想，
所以它是上限不是预测。

用法：python tools/think-structure-read.py tools/_runs/decode-split/<时间戳>
"""
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MIN_LEN = 12          # 太短的句子比出来没有意义（"所以"、"因此"）
COV_Q = 0.70          # 覆盖问句
COV_C = 0.80          # 覆盖系统提示
JAC_MAT = 0.60        # **能对上注入资料里的某一句话**（见 material_sents 的注释：
                      # 用"覆盖率"会把"用自己的话+材料的词"也算进来，实测假阳性 52%）
JAC_SELF = 0.60       # 与自己前文重复
JAC_ANS = 0.60        # 与最终正文重合 ⇒ 答案已定型

JAVA = os.path.join(os.path.dirname(HERE),
                    "rag-kb-service/src/main/java/com/kniv/ragkb/service/agent/AgenticRagService.java")


def bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def coverage(s, ref_set):
    """这句的二元组有多少落在参考集里 —— **不是 Jaccard**：复述资料时模型会加前缀
    （「[3] 提到：…」），Jaccard 会被前缀稀释，覆盖率不会。"""
    B = bigrams(s)
    return len(B & ref_set) / max(1, len(B))


def jac(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, len(A | B))


def contract_text():
    """从 Java 源码的文本块（`\"\"\"…\"\"\"`）里机械抠出**所有提示词文本**。

    抠出来的是全部文本块（含 PLAN_PROMPT 等），比"只要 ANSWER_SYSTEM"更宽 ——
    宽一点只可能把"复述契约"算多、不会算少，而我们要的正是"这些字是不是抄的提示词"。
    """
    try:
        src = io.open(JAVA, encoding="utf-8").read()
    except Exception:
        return ""
    blocks = re.findall(r'"""\s*(.*?)"""', src, re.S)
    return "\n".join(blocks)


def sentences(text, min_len=MIN_LEN):
    out, st = [], 0
    for m in re.finditer(r"[。！？；\n]+", text):
        e = m.end()
        if len(text[st:e].strip()) >= min_len:
            out.append((st, e, text[st:e]))
        st = e
    if len(text[st:].strip()) >= min_len:
        out.append((st, len(text), text[st:]))
    return out


def time_at(frames, off):
    cum, pt, pc = 0, None, 0
    for f in frames:
        t, c = f["t"] / 1000.0, cum + f["len"]
        if c >= off:
            if pt is None or c == pc:
                return t
            frac = (off - pc) / max(1, c - pc)
            return pt + frac * (t - pt)
        cum, pt, pc = c, t, c
    return frames[-1]["t"] / 1000.0 if frames else float("nan")


def material_sents(sources, C):
    """由 done 的 sources（docName/seq）反查**实际注入的那几块**的正文+语境行，
    切成**句子**返回。

    ⚠️ **为什么不是返回一个"二元组大集合"**（第一版就是那么写的，错的）：
    注入 11~19 段 ≈ 1~2 万字，大集合里什么词都有 ⇒ 模型**用自己的话+材料的词**
    造出来的句子覆盖率也能到 0.8+。实测那版判出的"复述资料"里
    **52% 的整句 Jaccard 不到 0.5**（样例：`2. G1在堆占用达到的比率时启动后台处理：`
    —— 那是它自己列的小标题，不是材料里的话）。
    改成"**能对上材料里的某一句话**"（整句 Jaccard），才有"复述"的语义。
    """
    idx = {}
    for i in range(C.n):
        idx.setdefault(C.doc[i], {})[str(C.seq[i])] = i
    txt, miss = [], 0
    for s in sources or []:
        i = idx.get(s.get("docName"), {}).get(str(s.get("seq")))
        if i is None:
            miss += 1
            continue
        txt.append(C.ctx[i] or "")
        txt.append(C.body[i])
    return [x for x in re.split(r"[。！？；\n]+", "\n".join(txt))
            if len(x.strip()) >= MIN_LEN], miss


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(glob.glob(os.path.join(d, "*-frames.json")))
    if not files:
        raise SystemExit(f"{d} 里没有 *-frames.json")

    from ruler import corpus
    C = corpus.load()
    CON = bigrams(contract_text())
    print(f"语料 {C.n} 块 · 戳 {C.stamp}　系统提示词参考集 {len(CON)} 个二元组")

    agg, profile = [], [[0.0] * 10 for _ in range(5)]
    KINDS = ["复述问句", "复述契约", "复述资料", "重复自己", "组织/推理"]

    for fp in files:
        meta = json.load(io.open(fp, encoding="utf-8"))
        think = io.open(fp.replace("-frames.json", "-thinking.txt"), encoding="utf-8").read()
        ans = io.open(fp.replace("-frames.json", "-answer.txt"), encoding="utf-8").read()
        frames = meta["frames"]
        ai = next((i for i, f in enumerate(frames) if f["ch"] == "ans"), None)
        if ai is None or not think:
            continue
        tf = frames[:ai]
        t0, t1 = tf[0]["t"] / 1000.0, frames[ai]["t"] / 1000.0
        span = max(1e-9, t1 - t0)

        MS, miss = material_sents(meta.get("sources"), C)
        QB = bigrams(meta["q"])

        sents = sentences(think)
        lab, secs, samples = [], [], {i: [] for i in range(len(KINDS))}
        earlier = []
        # **正文的到达曲线**，而不是"某一句对上"。
        #
        # 为什么换掉句级匹配（两个毛病，都是实测出来的）：
        #   ① **假阳性**：思考里复述问句、而正文首句也正好是问句 ⇒ 0.0s 就"命中"
        #      （令牌桶那题 Jaccard 0.82，但那一句不含任何答案内容）
        #   ② **长答案根本不触发**：759 字的正文，思考里的草稿与最终措辞差得够远，
        #      Jaccard 上不到 0.6 ⇒ 整题"没找到"，而那明显不是事实
        # 改成：把正文的**二元组集合**当目标，看累计想过多少 —— 于是得到一条
        # 单调上升的曲线，取它跨过 50% / 80% 的时刻。
        A = bigrams(ans)
        seen, curve = set(), []
        for a, b, s in sents:
            seen |= bigrams(s)
            curve.append((time_at(tf, a) - t0, len(seen & A) / max(1, len(A))))
        cross = {}
        for frac in (0.5, 0.8, 0.95):
            cross[frac] = next((t for t, c in curve if c >= frac), None)

        for a, b, s in sents:
            dt = time_at(tf, b) - time_at(tf, a)
            secs.append(dt)
            cq, cc = coverage(s, QB), coverage(s, CON)
            cm = max((jac(s, x) for x in MS), default=0.0)   # 能对上材料里的某一句话
            # **判定的优先级 = 从最特定的参考集到最泛的**：
            # 契约和问句都很短，命中它们说明这句基本是抄的；资料集很大，放最后。
            if cq >= COV_Q:
                k = 0
            elif cc >= COV_C:
                k = 1
            elif cm >= JAC_MAT:
                k = 2
            else:
                m = 0.0
                for ps in earlier:
                    m = max(m, jac(s, ps))
                k = 3 if m >= JAC_SELF else 4
            lab.append(k)
            earlier.append(s)
            if len(samples[k]) < 2:
                samples[k].append(s.strip()[:58])
            # 时间剖面：这一句落在思考窗口的哪一格
            frac = (time_at(tf, a) - t0) / span
            profile[k][min(9, int(frac * 10))] += dt

        buck = {}
        for k, dt in zip(lab, secs):
            buck[k] = buck.get(k, 0.0) + dt
        rep = sum(buck.get(k, 0.0) for k in range(4))

        print("\n" + "=" * 80)
        print(f"题：{meta['q'][:64]}")
        print(f"思考 {len(think)} 字 / 正文 {len(ans)} 字　窗口 {span:.1f}s"
              f"　注入 {len(meta.get('sources') or [])} 段（查不到 {miss}）")
        for k, name in enumerate(KINDS):
            dt = buck.get(k, 0.0)
            print(f"  {name:<8}{dt:>6.1f}s　{100*dt/span:>3.0f}%")
        line = f"  ── 复述类（前四项）合计 {100*rep/span:.0f}%　**正文内容想过："
        for frac in (0.5, 0.8, 0.95):
            t = cross[frac]
            line += f"{int(frac*100)}% @ " + (f"{t:.1f}s " if t is not None else "没到 ")
        if cross[0.8] is not None:
            line += f"⇒ 想到八成时还剩 {span-cross[0.8]:.1f}s（{100*(span-cross[0.8])/span:.0f}%）**"
        else:
            line += "**"
        print(line)
        for k, name in enumerate(KINDS):
            if samples[k]:
                print(f"     {name}样例：" + " ｜ ".join(samples[k]))
        agg.append({"q": meta["q"], "span": span, "think": len(think), "ans": len(ans),
                    "rep": rep, "c80": cross[0.8], "c50": cross[0.5],
                    "buck": [buck.get(k, 0.0) for k in range(5)]})

    if len(agg) < 2:
        return
    med = lambda v: sorted(v)[len(v) // 2]
    print("\n" + "=" * 80)
    print(f"跨题汇总（n={len(agg)}）　按**中位占比**（每题先算占比再取中位，避免长题吃掉平均）")
    for k, name in enumerate(KINDS):
        sh = [a["buck"][k] / a["span"] for a in agg]
        print(f"  {name:<8}中位 {100*med(sh):>3.0f}%　（{100*min(sh):.0f}%~{100*max(sh):.0f}%）")
    rep = [a["rep"] / a["span"] for a in agg]
    print(f"  ⇒ **复述类合计中位 {100*med(rep):.0f}%**")
    c80 = [a["c80"] / a["span"] for a in agg if a["c80"] is not None]
    if c80:
        print(f"  ⇒ **正文内容想到八成时，思考只用掉 {100*med(c80):.0f}% 的时间**"
              f"　⇒ 剩下 **{100*(1-med(c80)):.0f}%** 是「想完八成之后还在想」的时间"
              f"（早停类改动的上限，不是预计收益）")
        print(f"     逐题（八成线位置）：{', '.join(f'{100*x:.0f}%' for x in sorted(c80))}")
    print("\n  时间剖面（每 10% 思考窗口里的构成，行=成分、列=位置；数字是秒）")
    print("  " + " " * 10 + "".join(f"{i*10:>6}%" for i in range(10)))
    for k, name in enumerate(KINDS):
        row = profile[k]
        print(f"  {name:<8}" + "".join(f"{x:>7.1f}" for x in row))
    # **前/中/后三段各占多少** —— 结构那句话就从这里读出来
    print("\n  同一条剖面按**前/中/后三段**合并（每格=该成分有多少落在这一段）")
    print(f"  {'成分':<9}{'前 1/3':>9}{'中 1/3':>9}{'后 1/3':>9}")
    for k, name in enumerate(KINDS):
        row = profile[k]
        tot = sum(row) or 1.0
        a, b, c = sum(row[0:3]), sum(row[3:6]), sum(row[6:10])
        print(f"  {name:<9}{100*a/tot:>8.0f}%{100*b/tot:>8.0f}%{100*c/tot:>8.0f}%")
    print("\n  ⚠ 判据都是**文本覆盖/重合**，不是语义 —— 上面每题都打了样例，觉得不对就调阈值重跑"
          "（纯读文件，不重跑模型）。")


if __name__ == "__main__":
    main()
