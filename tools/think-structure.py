# -*- coding: utf-8 -*-
"""
**思考的结构与流程** —— 一把尺子，两个输入口。

## 它回答什么

一次问答里 **86% 的时间在 decode，其中 81% 的 token 是思考**。这个工具把那段思考
拆成五个视角，每个都**机械可判**、且**都能打样例让人核对**：

| 视角 | 问的是 |
|---|---|
| ① 五类（互斥） | 这段字是**复述问句 / 复述契约 / 复述资料 / 重复自己 / 组织推理**里的哪一类 |
| ② 两条正交轴 | 它**最终进正文了吗**（产出 or 过程）· 它**锚到具体块号了吗**（对着资料干活 or 自由） |
| ③ 交叉表 | 哪一类的思考最终进了正文 |
| ④ 时间形状 | 前/中/后三段里，各成分的分布 |
| ⑤ **流程** | 分两半：**它讲了多少**（起手词的占比）· **它实际怎么走**（五类的段序列与转移矩阵） |

## 两个输入口（同一套判据）

```powershell
python tools/think-structure.py --run tools/_runs/think-timeline/<时间戳>   # 帧表 ⇒ 有**秒**
python tools/think-structure.py --eval answer-quality __sw3                # 判分落盘 ⇒ 只有**字**
```

- `--run` 读探针留下的帧表（`decode-split-probe.mjs`）：**能算秒**，因为帧带到达时刻
- `--eval` 读 `tools/eval/_runs/*.json`：**只有字数** —— 依据是实测
  **思考与正文是同一个 decode 速率**（127.7 vs 129.7 字/秒），所以字数占比就是时间占比

两个口出来的百分比**可以直接比**；`--run` 额外的价值是能报秒与速率。

## 为什么这些判据长这样（三条踩出来的规矩）

1. **只用"已经给过它的文本"做比对**：复述的判据天然是"这段字有多少能在别处找到"。
   拿语义判据去判"这算不算浪费"在本项目里**反复指错方向**（`2026-09-21-少想这条线收口`
   那五条指标全被自己的数据推翻）。
2. **判据必须能打样例**：每一类都抽样打出来，觉得不对就调阈值重跑（纯读文件，不重跑模型）。
   第一版"复述资料"用覆盖率 ≥0.8，**假阳性 52%**（材料集一两万字，用自己的话+材料的词
   也能到 0.8）—— 把样例打出来才看见。
3. **流程要分两半，因为只有一半成立**：
   · **时间轴没有阶段** —— 20 格细时间轴**基本平坦**。早先按关键词分"阶段"的那版
     89% 落进"其它"，就是因为**假设它分阶段，它不分**
   · **起手词只覆盖 18%** —— 实测 78% 的句子没有起手词：**模型大部分时候不讲解自己
     在干什么，它就在干**。所以"过程自述"只能当**一个小口径**报，不能当流程
   · 真正的流程是**五类的段序列**：相邻同类合并成段、看段与段的转移。实测中位
     **26 段/题**，而主要循环是「重复自己 ⇄ 组织/推理」——**它不是流水线，是一个
     反复打磨的环**

用法补充：
    --trace 2      打印前 2 题的**流程轨迹**（压缩过的"它在干什么"）
    --json         机器可读（给别的工具/A-B 消费）
"""
import argparse
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ── 判据的阈值（**都是判断，不是刻度** —— 每个都配了样例）──────────────────
MIN_LEN = 12          # 太短的句子比出来没有意义（"所以"、"因此"）
COV_Q = 0.70          # 覆盖问句
COV_C = 0.80          # 覆盖系统提示
JAC_MAT = 0.60        # 能对上注入资料里的**某一句话**（用覆盖率会假阳性 52%）
JAC_SELF = 0.60       # 与前文重复
COV_ANS = 0.60        # 有多少落进最终正文 ⇒ 算"进了答案"

KINDS = ["复述问句", "复述契约", "复述资料", "重复自己", "组织/推理"]

# ── 流程的动作（标记词**取自真实样例**，见模块注释第 3 条）─────────────────
# 顺序即优先级：一句只归一个动作。
MOVES = [
    ("起手",     r"^首先|^让我先|^我需要(先)?理解|^用户的问题是|^问题问的是|^问题的关键"),
    ("逐块核对", r"在\s*\[?\d+\]?\s*(中|里)?提到|在参考资料中|资料\s*\[?\d|第\s*\d+\s*(段|块)|"
                 r"^\[?\d+\]?\s*(中|里)提到"),
    ("自检",     r"我需要确保|我要确保|需要确保|需要检查|不要|不能编造|必须|"
                 r"我将用中文|引用.{0,6}(标注|编号|格式)"),
    ("收束",     r"^所以|^综上|^总结|^最终|^我的回答|^现在(我)?(可以|来)写"),
]
JAVA = os.path.join(os.path.dirname(HERE),
                    "rag-kb-service/src/main/java/com/kniv/ragkb/service/agent/AgenticRagService.java")


# ── 基础件 ──────────────────────────────────────────────────────────────
def bigrams(s):
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def coverage(s, ref_set):
    """**不是 Jaccard**：复述时模型会加前缀（「[3] 提到：…」），Jaccard 会被前缀稀释。"""
    B = bigrams(s)
    return len(B & ref_set) / max(1, len(B))


def jac(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, len(A | B))


def sentences(text, min_len=MIN_LEN):
    out, st = [], 0
    for m in re.finditer(r"[。！？；\n]+", text or ""):
        e = m.end()
        if len((text or "")[st:e].strip()) >= min_len:
            out.append((st, e, text[st:e]))
        st = e
    if len((text or "")[st:].strip()) >= min_len:
        out.append((st, len(text), text[st:]))
    return out


def contract_text():
    """从 Java 源码的文本块里机械抠出**所有提示词文本**（比"只要 ANSWER_SYSTEM"宽 ——
    宽只会把这句算成复述、不会漏，而我们要的正是"这些字是不是抄的提示词"）。"""
    try:
        src = io.open(JAVA, encoding="utf-8").read()
    except Exception:
        return ""
    return "\n".join(re.findall(r'"""\s*(.*?)"""', src, re.S))


def move_of(s):
    for name, pat in MOVES:
        if re.search(pat, s):
            return name
    return "其它"


# ── 两个输入口 ──────────────────────────────────────────────────────────
def load_run(d):
    """探针运行目录 → [题]，**带帧表 ⇒ 能算秒**。"""
    out = []
    for fp in sorted(glob.glob(os.path.join(d, "*-frames.json"))):
        meta = json.load(io.open(fp, encoding="utf-8"))
        think = io.open(fp.replace("-frames.json", "-thinking.txt"), encoding="utf-8").read()
        ans = io.open(fp.replace("-frames.json", "-answer.txt"), encoding="utf-8").read()
        frames = meta.get("frames") or []
        ai = next((i for i, f in enumerate(frames) if f["ch"] == "ans"), None)
        if ai is None or not think:
            continue
        tf = frames[:ai]
        out.append({"q": meta.get("q", ""), "think": think, "ans": ans,
                    "sources": meta.get("sources") or [], "tf": tf,
                    "span": (frames[ai]["t"] - tf[0]["t"]) / 1000.0})
    return out


def load_eval(bench, tag):
    """判分落盘 → [题]，**只有字数**（无帧表）。依据见模块注释：速率恒定 ⇒ 占比即时间占比。"""
    pats = []
    for pat in (f"{bench}__qwen3-4b{tag}.json", f"{bench}__qwen3-4b__{tag}.json",
                f"{bench}__qwen3-4b*{tag}*.json"):
        pats = glob.glob(os.path.join(HERE, "eval", "_runs", pat))
        if pats:
            break
    if not pats:
        raise SystemExit(f"找不到落盘：{bench} / {tag}")
    p = sorted(pats)[-1]
    d = json.load(io.open(p, encoding="utf-8"))
    out = []
    for r in d["results"]:
        if not (r.get("thinking") or "").strip():
            continue
        out.append({"q": r.get("q", ""), "think": r["thinking"], "ans": r.get("answer") or "",
                    "sources": r.get("sources") or [], "tf": None, "span": 0.0})
    return out, os.path.basename(p)


# ── 分析核心 ────────────────────────────────────────────────────────────
class Case:
    """一题的完整拆解。**字数总是有；秒只在有帧表时才有。**"""

    def __init__(self, it, C, CON):
        self.q, self.think, self.ans = it["q"], it["think"], it["ans"]
        self.frames, self.raw_span = it["tf"], it["span"]
        self.pts = _axis(self.frames) if self.frames else None
        MS, self.miss = material_sents(it.get("sources"), C)
        QB = bigrams(self.q)
        A = bigrams(self.ans)
        self.sents = sentences(self.think)
        self.lab, self.mv, self.anch, self.in_ans = [], [], [], []
        self.w, self.t = [], []           # 字数 / 秒（秒可能为 None）
        earlier = []
        for a, b, s in self.sents:
            self.w.append(b - a)
            cq, cc = coverage(s, QB), coverage(s, CON)
            cm = max((jac(s, x) for x in MS), default=0.0)
            # 优先级：最特定的参考集在前（契约与问句都很短，命中说明基本是抄的）
            if cq >= COV_Q:
                k = 0
            elif cc >= COV_C:
                k = 1
            elif cm >= JAC_MAT:
                k = 2
            else:
                m = max((jac(s, x) for x in earlier), default=0.0)
                k = 3 if m >= JAC_SELF else 4
            self.lab.append(k)
            earlier.append(s)
            self.mv.append(move_of(s))
            self.anch.append(1 if re.search(r"\[\d{1,2}\]", s) else 0)
            self.in_ans.append(coverage(s, A))
        # 正文到达曲线（"想到八成"那一刻）
        seen, curve = set(), []
        for (a, b, s) in self.sents:
            seen |= bigrams(s)
            curve.append((_time_at(self.pts, a) - self.pts[0][1] if self.frames
                          else (b - 0) / max(1, len(self.think)),
                          len(seen & A) / max(1, len(A))))
        self.cross = {f: next((x for x, c in curve if c >= f), None) for f in (0.5, 0.8, 0.95)}
        # 秒轴上的跨度：合成起点 → 末句终点（有帧表时才非零）
        self.span = (sum(self.dt(i) for i in range(len(self.sents)))) if self.frames else 0.0

    def dt(self, i):
        """第 i 句的时长 —— 有帧表时是真秒，否则**用字数代替**（速率恒定，占比即占比）。"""
        if not self.frames:
            return float(self.w[i])
        a, b = self.sents[i][0], self.sents[i][1]
        return _time_at(self.pts, b) - _time_at(self.pts, a)

    # 便捷切片
    def unit(self):
        return "秒" if self.frames else "字"

    def total(self):
        return sum(self.dt(i) for i in range(len(self.sents))) or 1e-9


def _axis(frames):
    """(累计字数, 时刻) 序列 —— **首帧前补一个合成点**。

    思考是「攒够 60 字才推一帧」（`THINKING_FLUSH_CHARS`），所以**首帧自带的那 60 字
    是在它的到达时刻之前生成的**（那段落在 TTFT 里）。不补这个点，首句的时长会算成 0、
    整条时间轴缺开头约半秒 —— 实测第一次跑就撞上（轨迹第一行显示"0.0 秒 / 58 字"）。
    补法：按相邻帧的间隔往前推一格（没有第二帧就用 0.5 秒）。
    """
    pts, cum = [(0, frames[0]["t"] / 1000.0
                 - ((frames[1]["t"] - frames[0]["t"]) / 1000.0 if len(frames) > 1 else 0.5))], 0
    for f in frames:
        cum += f["len"]
        pts.append((cum, f["t"] / 1000.0))
    return pts


def _time_at(pts, off):
    for i in range(1, len(pts)):
        c, t = pts[i]
        if c >= off:
            pc, pt = pts[i - 1]
            return pt if c == pc else pt + (off - pc) / max(1, c - pc) * (t - pt)
    return pts[-1][1]


def material_sents(sources, C):
    """注入材料 → 句子列表。**按块正文+语境行**（与注入的内容一致）。"""
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


# ── 输出 ────────────────────────────────────────────────────────────────
def report(cases, trace=0, as_json=False):
    unit = cases[0].unit()
    T = [c.total() for c in cases]
    grand = sum(T)
    out = {"unit": unit, "n": len(cases)}

    # ① 五类
    per_kind = [sum(sum(c.dt(i) for i in range(len(c.sents)) if c.lab[i] == k)
                    for c in cases) for k in range(5)]
    out["kinds"] = {KINDS[k]: per_kind[k] / grand for k in range(5)}
    # ② 正交轴
    in_ans = sum(c.dt(i) for c in cases for i in range(len(c.sents)) if c.in_ans[i] >= COV_ANS)
    anch = sum(c.dt(i) for c in cases for i in range(len(c.sents)) if c.anch[i])
    out["in_answer"] = in_ans / grand
    out["anchored"] = anch / grand
    # ③ 交叉表
    out["cross"] = {KINDS[k]: [sum(c.dt(i) for c in cases for i in range(len(c.sents))
                                     if c.lab[i] == k and c.in_ans[i] >= COV_ANS) / grand,
                               sum(c.dt(i) for c in cases for i in range(len(c.sents))
                                   if c.lab[i] == k and c.in_ans[i] < COV_ANS) / grand]
                    for k in range(5)}
    # ⑤ 流程
    per_move = {}
    for c in cases:
        for i in range(len(c.sents)):
            per_move[c.mv[i]] = per_move.get(c.mv[i], 0.0) + c.dt(i)
    out["moves"] = {k: v / grand for k, v in per_move.items()}
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print(f"题 {len(cases)} 道 · 口径=**{unit}**"
          + ("（有帧表 ⇒ 报真秒）" if unit == "秒" else "（无帧表 ⇒ 报字数；速率恒定，占比即占比）"))
    print(f"\n① 五类（互斥）")
    for k in range(5):
        print(f"   {KINDS[k]:<8}{100*out['kinds'][KINDS[k]]:>5.0f}%")
    print(f"   ⇒ **复述类合计 {100*sum(out['kinds'][KINDS[k]] for k in range(4)):.0f}%**")
    print(f"\n② 正交轴（与①不互斥）")
    print(f"   **进了最终正文**　{100*out['in_answer']:>5.0f}%　← 产出")
    print(f"   没进正文　　　　{100*(1-out['in_answer']):>5.0f}%　← 过程（判断/核对/改措辞）")
    print(f"   **锚到块号**　　{100*out['anchored']:>5.0f}%　← 对着资料干活")
    print(f"\n③ 交叉表（哪一类的思考最终进了正文）")
    print(f"   {'成分':<10}{'进正文':>10}{'没进':>10}")
    for k, name in enumerate(KINDS):
        a, b = out["cross"][name]
        print(f"   {name:<10}{100*a:>9.0f}%{100*b:>9.0f}%")
    # ── ⑤ 流程 ────────────────────────────────────────────────────────
    #
    # **两半，因为实测只有一半成立**：
    #   a) 按"起手词"认过程自述 —— 实测**只占 ~18%**，其余 82% 的句子模型根本不讲解
    #      自己在干什么，它就在干（78% 的句子没有起手词）。**这一半是"它讲了多少"**。
    #   b) 真正的流程 = **五类的段序列** —— 相邻同类合并成段，段与段之间的转移才是一条
    #      可读的"它先做什么、再做什么"。**这一半才是流程**。
    narr = sum(v for k, v in out["moves"].items() if k != "其它")
    print(f"\n⑤ 流程")
    print(f"   (a) 过程自述（有起手词的句子）占 **{100*narr:.0f}%**："
          + "　".join(f"{k} {100*v:.0f}%" for k, v in out["moves"].items() if k != "其它"))
    print(f"       ⇒ **模型大部分时候不讲解自己在干什么，它就在干**")
    runs = []
    for c in cases:
        r = []
        for i in range(len(c.sents)):
            if not r or r[-1] != c.lab[i]:
                r.append(c.lab[i])
        runs.append(r)
    rl = sorted(len(r) for r in runs)
    print(f"   (b) 真实流程 = 五类的**段序列**：中位 **{rl[len(rl)//2]} 段/题**"
          f"（{rl[0]}~{rl[-1]}）")
    trans = {}
    for r in runs:
        for i in range(1, len(r)):
            trans[(r[i - 1], r[i])] = trans.get((r[i - 1], r[i]), 0) + 1
    tot_t = sum(trans.values()) or 1
    print(f"       最常见的转移（段→段）：")
    for (x, y), n in sorted(trans.items(), key=lambda kv: -kv[1])[:6]:
        print(f"         {KINDS[x]} → {KINDS[y]}　{n} 次（{100*n/tot_t:.0f}%）")
    # 前/中/后三段
    print(f"\n④ 时间形状（前/中/后三段里各成分的占比）")
    print(f"   {'成分':<10}{'前 1/3':>9}{'中 1/3':>9}{'后 1/3':>9}")
    for k, name in enumerate(KINDS):
        seg = [0.0, 0.0, 0.0]
        for c in cases:
            n = len(c.sents)
            for i in range(n):
                if c.lab[i] != k:
                    continue
                x = i / max(1, n - 1)
                seg[0 if x < 1 / 3 else (1 if x < 2 / 3 else 2)] += c.dt(i)
        tot = sum(seg) or 1e-9
        print(f"   {name:<10}{100*seg[0]/tot:>8.0f}%{100*seg[1]/tot:>8.0f}%{100*seg[2]/tot:>8.0f}%")

    # 流程轨迹（--trace N）：**按五类的段序列**打印，不是按起手词
    # ——起手词只覆盖 18%，照着它打会得到一条几乎全是"其它"的轨迹（实测过）。
    for c in cases[:trace]:
        print(f"\n── 流程轨迹：{c.q[:56]}")
        runs = []
        for i in range(len(c.sents)):
            if runs and runs[-1][0] == c.lab[i]:
                runs[-1][1] += c.dt(i)
                runs[-1][2] += c.w[i]
                runs[-1][3] += 1
                if c.mv[i] != "其它":
                    runs[-1][4] = c.mv[i]
            else:
                runs.append([c.lab[i], c.dt(i), c.w[i], 1,
                             c.mv[i] if c.mv[i] != "其它" else ""])
        acc = 0.0
        for lab, s_, w_, n_, mv_ in runs:
            print(f"   {acc:>6.1f}{unit}　{KINDS[lab]:<8}{s_:>6.1f}{unit} / {w_:>5} 字 / {n_:>2} 句"
                  + (f"　[{mv_}]" if mv_ else ""))
            acc += s_
    if trace:
        print("\n   （第一列是累计到该段开头的时刻；[方括号]里是那段的起手词，多数段没有）")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--run")
    ap.add_argument("--eval", nargs=2, metavar=("BENCH", "TAG"))
    ap.add_argument("--trace", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    a, _ = ap.parse_known_args()

    from ruler import corpus
    C = corpus.load()
    if a.run:
        items = load_run(a.run)
        src = a.run
    elif a.eval:
        items, src = load_eval(*a.eval)
    else:
        raise SystemExit(__doc__)
    if not items:
        raise SystemExit(f"{src} 里没有可分析的思考")
    CON = bigrams(contract_text())
    cases = [Case(it, C, CON) for it in items]
    print(f"语料 {C.n} 块 · 戳 {C.stamp}　来源 {src}")
    report(cases, trace=a.trace, as_json=a.json)


if __name__ == "__main__":
    main()
