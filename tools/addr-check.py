# -*- coding: utf-8 -*-
"""
**地址计划这把尺子**：模型用了地址没有？用得对不对？是**替代**了抄写还是**加在**抄写之上？

## 三个量，都机械可判

1. **用了没有**：思考里 `⟨n.m⟩` 的出现次数、密度（每千字）
2. **对不对**：`n` 有没有超出注入的段数、`m` 有没有超出该段的句数
   （实测抓到过 `⟨1?⟩` —— 模型编了个不存在的地址；地址错 = 指错资料，必须能数出来）
3. **替代还是叠加** —— **这一条决定这个方向成不成立**：
   看地址**后面紧跟的 60 字**能不能对上材料里的一句。
   · 对得上 ⇒ 「从⟨1.7⟩中：令牌桶限制的是平均流入速率…」= **指代 + 照抄**（加法，越用越亏）
   · 对不上 ⇒ 「令牌允许突发：[⟨1.7⟩、⟨2.5⟩]」= **纯指代**（替代，才是要的）

## 判据要能核对

第 3 条每类都打样例。**别只看比例** —— 这个项目在"机械指标指错方向"上栽过五次
（`2026-09-21-少想这条线收口`），所以尺子必须能和原文对上。

用法：python tools/addr-check.py <eval落盘名> [tag]
     python tools/addr-check.py answer-quality qwen3:4b __sa__armB
"""
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "eval", "_runs")
sys.path.insert(0, HERE)

ADDR = re.compile(r"⟨\s*(\d+)\s*[.．]\s*(\d+)\s*⟩")
FOLLOW = 60          # 地址后面看这么多字
JAC = 0.45           # 后面那段字与材料某句的重合达到这个数 ⇒ 算"跟着抄了"


def bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def jac(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, len(A | B))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    bench, model, tag = (sys.argv[1], "qwen3:4b", "") if len(sys.argv) < 3 else \
                        (sys.argv[1], re.sub(r"[:/]", "-", sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
    pats = []
    for pat in (f"{bench}__{model}{tag}.json", f"{bench}__{model}__{tag}.json",
                f"{bench}__{model}*{tag}*.json"):
        pats = glob.glob(os.path.join(RUNS, pat))
        if pats:
            break
    if not pats:
        raise SystemExit(f"找不到落盘：{bench} / {model} / {tag}")
    path = sorted(pats)[-1]
    d = json.load(io.open(path, encoding="utf-8"))
    print(f"{os.path.basename(path)}　{len(d['results'])} 题\n")

    # 注入材料：由 sources（docName/seq）反查——与 think-structure-eval 同一套
    from ruler import corpus
    C = corpus.load()
    idx = {}
    for i in range(C.n):
        idx.setdefault(C.doc[i], {})[str(C.seq[i])] = i
    srows = corpus.psql_rows("SELECT chunk_id, seq, text FROM sentences ORDER BY chunk_id, seq")
    bys = {}
    for cid, sq, t in srows:
        bys.setdefault(int(cid), []).append((int(sq), t))

    tot_think = tot_addr = tot_bad = tot_follow = 0
    samples_follow, samples_pure, samples_bad = [], [], []
    for r in d["results"]:
        think = r.get("thinking") or ""
        if not think:
            continue
        tot_think += len(think)
        # 这一题注入的段 → 每段有几句
        nsent = {}
        for n, s in enumerate(r.get("sources") or [], start=1):
            i = idx.get(s.get("docName"), {}).get(str(s.get("seq")))
            if i is not None:
                nsent[n] = len(bys.get(int(C.ids[i]), []))
        ms = []
        for s in r.get("sources") or []:
            i = idx.get(s.get("docName"), {}).get(str(s.get("seq")))
            if i is not None:
                ms.append(C.body[i])
        mat = "\n".join(ms)

        for m in ADDR.finditer(think):
            n, q = int(m.group(1)), int(m.group(2))
            tot_addr += 1
            ok = n in nsent and 1 <= q <= nsent[n]
            if not ok:
                tot_bad += 1
                if len(samples_bad) < 3:
                    samples_bad.append(think[max(0, m.start() - 30):m.end() + 30].replace("\n", "⏎"))
            after = think[m.end():m.end() + FOLLOW]
            # 后面那 60 字里，有没有一大段能和材料某句对上
            hit = False
            for seg in re.split(r"[。！？；\n]+", after):
                if len(seg.strip()) >= 12 and jac(seg, mat) > 0.02 and \
                        any(jac(seg, x) >= JAC for x in re.split(r"[。！？；\n]+", mat) if len(x.strip()) >= 12):
                    hit = True
                    break
            if hit:
                tot_follow += 1
                if len(samples_follow) < 3:
                    samples_follow.append(think[m.start():m.end() + FOLLOW].replace("\n", "⏎"))

    if not tot_addr:
        print("思考里一处地址都没有 —— 模型没用起来（开关没生效？或提示词没送到？）")
        return
    print(f"思考合计 {tot_think} 字，地址出现 **{tot_addr}** 次"
          f"（{1000*tot_addr/max(1,tot_think):.1f} 次/千字）")
    print(f"  **地址指错** {tot_bad} 次（{100*tot_bad/tot_addr:.0f}%）"
          f" —— 指到不存在的段/句")
    print(f"  **指代后跟着抄材料** {tot_follow} 次（{100*tot_follow/tot_addr:.0f}%）")
    print(f"  ⇒ **纯指代** {100*(tot_addr-tot_follow)/tot_addr:.0f}%"
          f"　（这一档才是「替代」；另一档是「叠加」，越用越贵）")
    for name, ss in (("指代+抄（叠加）", samples_follow), ("纯指代", None), ("地址指错", samples_bad)):
        if ss:
            print(f"\n  {name} 样例：")
            for s in ss:
                print(f"    …{s[:110]}…")


if __name__ == "__main__":
    main()
