# -*- coding: utf-8 -*-
"""**判类问法扫描**：逐块那句问法换一种，看【乙/丙】的分界往哪挪。

## 为什么做这个（2026-09-26）

判类把【乙/丙】定在"**逐块**问『这段能**直接回答**上面的问题吗』、取最大 > 0.5"。
可**部分可答**的题（`grounded-partial`，现实里最常见）需要多块**合起来**才答得了整个问题
⇒ 单独问哪一块都是"不能" ⇒ 判成【丙】。实测：10 道部分可答题里 **6 道判丙**，
而判丙的答案 **100% 开口就是「知识库中没有」**（237/237 条样本）。

## 这次要回答的两件

1. **换个问法能不能把这几道挪回【乙】** —— 问法从"能不能答完"改成"有没有用"
2. **代价是什么** —— 变松之后，本来该判丙的题会不会也跟着变成乙
   （"资料只是沾边却硬答"是另一个方向的错，台账里有先例）

判据（不预设答案，**逐题看数**）：
  · `grounded`（该乙）与 `ungrounded`（该丙）**必须不动** —— 它们的余量很大：
    乙类 0.889~0.998、丙类 0.002~0.268，**中间是空的** ⇒ 换问法如果把它们挪过 0.5，就是坏了
  · `grounded-partial` 逐题看 P 值怎么动；目标题是**资料里确实有答案值**的那两道
    （aq-p3 布隆过滤器命令 / aq-p5 B-tree 最大层数 —— 台账里"资料确实写了 3 层"那道）
  · **反例**：aq-p2 / aq-p6 判丙是对的（资料最高块 P≈0.004 / 0.309 且答案里没有资料值）

两种问法各跑一遍全部 21 题 × 每题的来源块（逐块一次调用，只读一个 token）。
缓存**按问法分开**（`_cache-chunkp-<tag>.json`）—— 换问法后概率不可复用。

用法：python tools/eval/category-phrasing-probe.py
"""
import importlib.util
import io
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_spec = importlib.util.spec_from_file_location("logprob_judge",
                                               os.path.join(TOOLS, "logprob-judge.py"))
lpj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lpj)

BASE_RUN = os.path.join(HERE, "_runs", "answer-quality__qwen3-4b.json")

# 该判乙的题 / 该判丙的题（判据的两端，**换问法不能把它们挪过 0.5**）
MUST_YI = {"aq-g1", "aq-g2", "aq-g3", "aq-g4", "aq-g5", "aq-g6"}
MUST_BING = {"aq-u1", "aq-u2", "aq-u3", "aq-u4", "aq-u5", "aq-u6"}
# 部分可答里**资料确实有答案值**的两道（目标：挪回乙）；另两道判丙是对的
PARTIAL_WANT_YI = {"aq-p3", "aq-p5"}
PARTIAL_OK_BING = {"aq-p2", "aq-p6"}


def chunk_text(C, s):
    i = next((k for k in range(C.n)
              if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
    return C.body[i] if i is not None else (s.get("preview") or "")


def probs(C, rows, ask, tag):
    cache_p = os.path.join(HERE, f"_cache-chunkp-{tag}.json")
    cache = json.load(io.open(cache_p, encoding="utf-8")) if os.path.exists(cache_p) else {}
    out = {}
    for x in rows:
        ps = []
        for s in (x.get("sources") or []):
            key = f"{x['id']}|{s.get('docName')}#{s.get('seq')}"
            if key not in cache:
                cache[key] = lpj.judge_relevance(x["q"], chunk_text(C, s), ask=ask)
                json.dump(cache, io.open(cache_p, "w", encoding="utf-8"))
            p = cache[key]
            if p is not None:
                ps.append(p)
        out[x["id"]] = (max(ps) if ps else 0.0, sorted(ps, reverse=True)[:5], len(ps))
    return out


def main():
    from ruler import corpus
    C = corpus.load()
    rows = json.load(io.open(BASE_RUN, encoding="utf-8"))["results"]
    A = probs(C, rows, lpj.ASK_DIRECT, "direct")
    B = probs(C, rows, lpj.ASK_PARTIAL, "partial")

    kind = {x["id"]: x["kind"] for x in rows}
    q = {x["id"]: x["q"] for x in rows}

    def verdict(p):
        return "乙" if p > 0.5 else "丙"

    print(f"{'题':<8}{'题型':<18}{'现问法':>8}{'判定':>6}{'':>4}{'新问法':>8}{'判定':>6}  问句")
    broke = []
    for cid in [x["id"] for x in rows]:
        pa, pb = A[cid][0], B[cid][0]
        va, vb = verdict(pa), verdict(pb)
        mark = ""
        if cid in MUST_YI and vb != "乙":
            mark = "  ⚠️该乙却判丙"
            broke.append(cid)
        if cid in MUST_BING and vb != "丙":
            mark = "  ⚠️该丙却判乙"
            broke.append(cid)
        print(f"{cid:<8}{kind[cid]:<18}{pa:>8.3f}{va:>6}{'':>4}{pb:>8.3f}{vb:>6}"
              f"  {q[cid][:24]}{mark}")

    hi = {c: B[c][1][:3] for c in PARTIAL_WANT_YI | PARTIAL_OK_BING}
    print("\n部分可答那几道的**前三高块**（新问法）：")
    for c in sorted(hi):
        tgt = "想要乙" if c in PARTIAL_WANT_YI else "丙是对的"
        print(f"  {c}（{tgt}）：{[round(x, 3) for x in hi[c]]}")

    print(f"\n两端的题被挪过 0.5 的：{broke or '无 ✓'}")
    print("目标两道（aq-p3/aq-p5）："
          + "　".join(f"{c} {A[c][0]:.3f}→{B[c][0]:.3f}" for c in sorted(PARTIAL_WANT_YI)))

    # ── ③ **整批材料这一层问**（一次调用，不是逐块）──────────────────────
    cache_m = os.path.join(HERE, "_cache-material.json")
    cm = json.load(io.open(cache_m, encoding="utf-8")) if os.path.exists(cache_m) else {}
    M = {}
    for x in rows:
        if x["id"] not in cm:
            mat = "\n".join(chunk_text(C, s)[:400] for s in (x.get("sources") or [])[:12])
            cm[x["id"]] = lpj.judge_material(x["q"], mat)
            json.dump(cm, io.open(cache_m, "w", encoding="utf-8"))
        M[x["id"]] = cm[x["id"]]
    print(f"\n{'题':<8}{'题型':<18}{'整批材料 P(能答)':>16}{'判定':>6}  问句")
    broke2 = []
    for cid in [x["id"] for x in rows]:
        p = M.get(cid)
        v = "?" if p is None else ("乙" if p > 0.5 else "丙")
        bad = ""
        if cid in MUST_YI and v != "乙":
            bad, _ = "  ⚠️该乙却判丙", broke2.append(cid)
        if cid in MUST_BING and v != "丙":
            bad, _ = "  ⚠️该丙却判乙", broke2.append(cid)
        print(f"{cid:<8}{kind[cid]:<18}{(round(p,3) if p is not None else '—'):>16}{v:>6}"
              f"  {q[cid][:24]}{bad}")
    print(f"两端被挪过 0.5 的：{broke2 or '无 ✓'}"
          f"　｜ 目标两道："
          + "　".join(f"{c} {M.get(c)}" for c in sorted(PARTIAL_WANT_YI)))


if __name__ == "__main__":
    main()
