# -*- coding: utf-8 -*-
"""
**在 A/B 的落盘结果上量思考结构** —— 不用重跑模型，也不用帧表。

`think-structure-read.py` 读的是探针留下的帧表（能算秒），本脚本读的是
`tools/eval/_runs/*.json`（判分落盘）。两者用**同一套判据**，区别只有一处：

    **这里只报"字数占比"，不报秒。** 依据是 2026-09-22 实测：
    **思考与正文是同一个 decode 速率**（127.7 vs 129.7 字/秒，n=6）
    ⇒ 字数占比**就是**时间占比。（帧表那支能直接验证这一点，所以这个外推不悬空。）

为什么要单独一个脚本：`ab.ps1` 每臂落一个 `--tag`，两臂的思考文本都在盘上，
于是"改了提示词之后思考的结构变没变"是**读文件**的事，不必再花钱跑一遍。

用法：
    python tools/think-structure-eval.py answer-quality qwen3:4b r1__armA r1__armB
    python tools/think-structure-eval.py answer-quality qwen3:4b r1__armA     # 单臂也看
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

MIN_LEN = 12
COV_Q = 0.70
JAC_MAT = 0.60
JAC_SELF = 0.60
KINDS = ["复述问句", "复述资料", "重复自己", "组织/推理"]


def bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def jac(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, len(A | B))


def coverage(s, ref):
    B = bigrams(s)
    return len(B & ref) / max(1, len(B))


def sents(t):
    return [x for x in re.split(r"[。！？；\n]+", t) if len(x.strip()) >= MIN_LEN]


def load_corpus():
    from ruler import corpus
    return corpus.load()


def measure(res, C, idx):
    """一条答案 → 各成分的字数。**判据与 think-structure-read.py 逐字相同。**"""
    think = res.get("thinking") or ""
    if not think.strip():
        return None
    ms = []
    for s in res.get("sources") or []:
        i = idx.get(s.get("docName"), {}).get(str(s.get("seq")))
        if i is None:
            continue
        ms += sents(C.ctx[i] or "")
        ms += sents(C.body[i])
    qb = bigrams(res.get("q") or "")
    out = {k: 0 for k in KINDS}
    earlier = []
    cited = 0          # 含 [n] 引用标记的字数 —— 与上面的分类**正交**
    for seg in sents(think):
        n = len(seg)
        if coverage(seg, qb) >= COV_Q:
            k = KINDS[0]
        elif ms and max(jac(seg, x) for x in ms) >= JAC_MAT:
            k = KINDS[1]
        elif earlier and max(jac(seg, x) for x in earlier) >= JAC_SELF:
            k = KINDS[2]
        else:
            k = KINDS[3]
        out[k] += n
        # **「逐条扫描」这笔开销在哪** —— 复述资料量的是"抄原句"，而模型扫块时写的
        # 多是自己的话（「[3] 提到了…但没有…」）⇒ 会被算进"组织/推理"里去。
        # 所以单独量一条：有多少字是**挂在某个具体块号上**的。
        if re.search(r"[\[［]\s*\d+\s*[\]］]", seg):
            cited += n
        earlier.append(seg)
    return {"total": len(think), "buckets": out, "cited": cited,
            "ans": len(res.get("answer") or ""), "ms": res.get("ms") or 0,
            "ttft": res.get("ttftMs") or 0}


def summarize(path, C, idx, only_kind=None):
    d = json.load(io.open(path, encoding="utf-8"))
    rs = [r for r in d["results"] if not r.get("error")]
    if only_kind:
        rs = [r for r in rs if (r.get("kind") or "") == only_kind]
    rows = [m for m in (measure(r, C, idx) for r in rs) if m]
    if not rows:
        return None
    n = len(rows)
    tot = max(1, sum(r["total"] for r in rows))
    return {
        "n": n,
        "total": sum(r["total"] for r in rows) / n,
        "ans": sum(r["ans"] for r in rows) / n,
        "share": {k: sum(r["buckets"][k] for r in rows) / tot for k in KINDS},
        "cited": sum(r["cited"] for r in rows) / tot,
        "ms": sum(r["ms"] for r in rows) / n / 1000.0,
        "ttft": sum(r["ttft"] for r in rows) / n / 1000.0,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    # 落盘文件名里的模型名是**净化过**的：`qwen3:4b` → `qwen3-4b`（冒号也换掉）。
    # 只换斜杠是不够的 —— 第一次跑就栽在这里，报"找不到 tag"而其实文件就在旁边。
    bench, model = sys.argv[1], re.sub(r"[:/]", "-", sys.argv[2])
    tags = sys.argv[3:]
    C = load_corpus()
    idx = {}
    for i in range(C.n):
        idx.setdefault(C.doc[i], {})[str(C.seq[i])] = i
    print(f"语料 {C.n} 块 · 戳 {C.stamp}")

    res = []
    for tag in tags:
        # **三种名字都得认**：`runner.run_path` 是把 tag **接在模型名后面**的
        # （`...qwen3-4b__armA.json`），而 repeat>1 时又会插一层 `__r1`。
        # 第一次跑就栽在"报找不到、其实文件就在旁边"上 —— 这类失败必须写全。
        pats = []
        for pat in (f"{bench}__{model}{tag}.json", f"{bench}__{model}__{tag}.json",
                    f"{bench}__{model}*{tag}*.json"):
            pats = glob.glob(os.path.join(RUNS, pat))
            if pats:
                break
        if not pats:
            print(f"  找不到 tag={tag} 的落盘（{bench} / {model}）")
            continue
        s = summarize(sorted(pats)[-1], C, idx)
        if s:
            res.append((tag, s, os.path.basename(sorted(pats)[-1])))

    print(f"\n{'臂':<14}{'n':>4}{'思考字':>8}{'正文字':>8}{'TTFT':>7}{'总耗时':>8}"
          + "".join(f"{k:>9}" for k in KINDS) + f"{'挂块号':>9}")
    for tag, s, fn in res:
        print(f"{tag:<14}{s['n']:>4}{s['total']:>8.0f}{s['ans']:>8.0f}"
              f"{s['ttft']:>6.1f}s{s['ms']:>7.1f}s"
              + "".join(f"{100*s['share'][k]:>8.0f}%" for k in KINDS))

    # **按题型拆开**（必须）—— 这把 bench 是 6 grounded + 6 grounded-partial
    # + 6 ungrounded + 3 chitchat，而后 9 题**本来就没有可复述的资料**。
    # 合在一起算会把"复述资料"稀释掉：实测整体 6~15%，而 grounded 那一档才是要看的地方。
    kinds = []
    for tag, _, fn in res:
        d = json.load(io.open(os.path.join(RUNS, fn), encoding="utf-8"))
        for r in d["results"]:
            k = r.get("kind")
            if k and k not in kinds:
                kinds.append(k)
    if kinds:
        print("\n按题型（只看**有资料可复述**的那几档）：")
        print(f"{'题型':<18}{'臂':<12}{'n':>3}{'思考字':>8}" + "".join(f"{k:>9}" for k in KINDS))
        for k in kinds:
            for tag, _, fn in res:
                s = summarize(os.path.join(RUNS, fn), C, idx, only_kind=k)
                if not s or not s["n"]:
                    continue
                print(f"{k:<18}{tag:<12}{s['n']:>3}{s['total']:>8.0f}"
                      + "".join(f"{100*s['share'][x]:>8.0f}%" for x in KINDS))

    if len(res) >= 2:
        (t1, a, _), (t2, b, _) = res[0], res[1]
        print(f"\n两臂之差（{t2} − {t1}）：")
        print(f"  思考字数 {b['total']-a['total']:+.0f} 字"
              f"（{100*(b['total']-a['total'])/max(1e-9,a['total']):+.0f}%）"
              f"　正文 {b['ans']-a['ans']:+.0f} 字　总耗时 {b['ms']-a['ms']:+.1f}s")
        for k in KINDS:
            print(f"  {k:<8}{100*a['share'][k]:>5.0f}% → {100*b['share'][k]:>5.0f}%"
                  f"　（{100*(b['share'][k]-a['share'][k]):+.1f} 点）")
        print(f"  {'挂块号':<8}{100*a['cited']:>5.0f}% → {100*b['cited']:>5.0f}%"
              f"　（{100*(b['cited']-a['cited']):+.1f} 点）")
        print("\n  ⚠ 单次重复的噪声底 3~33%（见 2026-09-22 位置探针）——"
              "两臂差在个位数点数以内不要当结论。")
    print("\n  判据：句级，二元组 Jaccard/覆盖；与 tools/think-structure-read.py 逐字相同。"
          "\n  样例核对：python tools/think-structure-read.py <探针运行目录>")


if __name__ == "__main__":
    main()
