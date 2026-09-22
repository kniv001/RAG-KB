# -*- coding: utf-8 -*-
"""
**尺子的唯一入口**。以后跑评测不再需要写新脚本、也不再需要动题目。

  python tools/ruler.py audit                 # 所有题目集 + 语料新鲜度，全自检
  python tools/ruler.py rebuild               # 重建向量缓存（语料变了才需要）
  python tools/ruler.py migrate               # 一次性：旧题目集 → 锚内容格式
  python tools/ruler.py ls                    # 有哪些尺子
  python tools/ruler.py run multihop-25 --k 8,12,16 --cap 24
  python tools/ruler.py gate                  # 四条回归门一起跑
  python tools/ruler.py seg                   # 分段尺子自检
  python tools/ruler.py seg --all             # 评所有候选分段（tools/_bounds_*.json）
  python tools/ruler.py vocab                 # 词面缺口：召不回是不是因为"用词对不上"

`run` 的旋钮（＝历史上逐个试过的那些）：
  --k 8,12,16        每条查询取几个
  --cap 12,24,48     融合后装几个（≈ max-contexts）
  --kind ctx+body    索引文本（ctx+body / body / ctx）
  --thresh 0.60      距离门槛（1-cos），不设＝不滤
  --mmr cos:0.90     冗余过滤，cos:阈值 或 lit:阈值
  --source planner   用哪批查询（planner＝规划器真实产出 / raw＝原始问句）
  --repeat 3         重复几次（真身那类高噪声尺子要 ≥3）
  --pair             以第一条臂为基准做配对比较（逐题比，给符号检验）

**每个数字都带尺子名与语料戳** —— 判死不能脱离尺子引用（2026-09-20 的教训）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ruler import cases, corpus, report, retr                     # noqa: E402


def _nums(v):
    return [x.strip() for x in str(v).split(",") if x.strip()]


def cmd_ls():
    C = corpus.load()
    print(C.line() + "\n")
    for nm in cases.names():
        import io
        import json
        d = json.load(io.open(os.path.join(cases.CASE_DIR, nm + ".json"), encoding="utf-8"))
        print(f"  {nm:<20} {len(d['cases']):>3} 题　判据 {d.get('judge','all-hit'):<10}"
              f"　{d.get('note','')[:52]}")


def cmd_audit():
    C = corpus.load()
    print(C.line())
    # ① 语料戳与缓存是否一致（不一致会触发重建，先说明白）
    for kind in ("ctx+body", "body", "ctx"):
        import json
        p = os.path.join(corpus.TOOLS, corpus.KINDS[kind])
        st = ids = None
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                st, ids = d.get("stamp"), d.get("ids")
            except Exception:
                st = "读不动"
        if st == C.stamp:
            mark = "✓"
        elif st is None and ids == C.ids:
            mark = "✓ 旧格式（ids 对得上，跑 rebuild 补戳）"     # 这一份可信，只是没有戳
        elif st is None:
            mark = "✗ 旧格式且 ids 对不上（跑 rebuild）"
        else:
            mark = "✗ 对不上（跑 rebuild）"
        print(f"  向量缓存 {kind:<9} 戳 {st or '无'}　{mark}")
    print()
    bad = 0
    for nm in cases.names():
        try:
            cases.load(nm, C, strict=True)
        except SystemExit as e:
            bad += 1
            print(f"  ✗ {nm}：{e}")
    print(f"\n题目集 {len(cases.names())} 个，通过 {len(cases.names()) - bad} 个"
          f"{'，**有不过的，先修题目**' if bad else ''}")
    return bad


def cmd_rebuild():
    C = corpus.load()
    print(C.line())
    for kind in ("ctx+body", "body", "ctx"):
        C.vecs(kind)
    print("三个变体都对齐到当前语料戳了")


def cmd_migrate():
    from ruler import migrate
    migrate.run()


def _arms(C, cs, a):
    ks = _nums(a["k"]) if a.get("k") else [None]
    pairstyle = a.get("cap") == "same"       # cap 跟着 k 走（单查询尺子就是这样：取 k 就是看 k）
    caps = _nums(a["cap"]) if (a.get("cap") and not pairstyle) else [None]
    kinds = _nums(a["kind"]) if a.get("kind") else [None]      # 可多值：--kind ctx+body,body
    arms = []
    pairs = list(zip(ks, ks)) if pairstyle else \
        [(k, c) for k in ks for c in caps]
    for k, cap in pairs:
      for kd in kinds:
        cfg = {}
        if k is not None:
            cfg["k"] = int(k)
        if cap is not None:
            cfg["cap"] = int(cap)
        if kd:
            cfg["kind"] = kd
        if a.get("thresh"):
            cfg["thresh"] = float(a["thresh"])
        if a.get("mmr"):
            kd, tv = a["mmr"].split(":")
            cfg["mmr"] = (kd, float(tv))
        if a.get("source"):
            cfg["query_source"] = a["source"]
        arms.append(("vec", retr.vec, cfg))
    return arms


def cmd_run(argv):
    if not argv:
        raise SystemExit("用法：python tools/ruler.py run <题目集> [--k 8,12,16] [--cap 24]")
    name, argv = argv[0], argv[1:]
    a = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:]
            if key == "pair":
                a["pair"] = True
                i += 1
            else:
                a[key] = argv[i + 1]
                i += 2
        else:
            i += 1
    C = corpus.load()
    cs = cases.load(name, C, strict=True)
    rec = None
    if a.get("source", "planner") == "planner" and name.startswith("multihop"):
        import io
        import json
        # `--rec <路径>` 可以指到**别处录的一批规划结果**（例如 plan-capture.py 多跑几次
        # 存下来的那些）—— 换一批查询就是换一组独立样本，不必重写这个文件。
        p = a.get("rec") or os.path.join(corpus.TOOLS, "_multihop-live.json")
        if os.path.exists(p):
            rec = json.load(io.open(p, encoding="utf-8"))
            a.setdefault("source", "planner")
    arms = _arms(C, cs, a)
    detail = report.table(C, cs, arms, repeats=int(a.get("repeat", 1)), rec=rec)
    if a.get("pair") and len(arms) > 1:
        # detail 的键是**带参数的标签**（report.table 里现算的），不是裸名字 —— 要用同一个函数拼
        labels = [report.arm_label(nm, cfg) for nm, _, cfg in arms]
        report.paired(C, cs, labels[0], detail[labels[0]], labels[1], detail[labels[1]])


def cmd_gate():
    """四条回归门。**顺序跑，每条自带语料戳** —— 一条不过就停。"""
    C = corpus.load()
    print(f"===== 回归门 · {C.line()} =====")
    # 每条门的形状写清楚：多跳是**原始问句单查询**（与 multihop-probe 同形，数字能接上历史）；
    # 单跳/难题/自检索是**取 k 看 k**。形状不同数字就不可比，所以在这里显式钉死。
    for nm, a in [("single-hop-15", {"k": "8,24", "cap": "same", "source": "raw"}),
                  ("hard-query-15", {"k": "8,24", "cap": "same", "source": "raw"}),
                  ("multihop-25", {"k": "8,12,24", "cap": "same", "source": "raw"}),
                  ("xdoc-8", {"k": "8,24", "cap": "same", "source": "raw"}),
                  ("selfretrieval-23", {"k": "1,10", "cap": "same", "source": "raw"})]:
        print(f"\n######## {nm}")
        cs = cases.load(nm, C, strict=True)
        report.table(C, cs, _arms(C, cs, a))
    print("\n（长上下文埋针不在这一族：它测的是硬件，见 tools/kv-quant-needle-probe.py）")


def cmd_seg(argv):
    """分段族：**先自检尺子，再评候选分段**。

    候选分段的来源是文件（`tools/_bounds_*.json`，里面的 `bounds` 是 1-based 段号）。
    算法怎么产生这些边界不归尺子管 —— 尺子只负责**量得一样**。
    """
    import glob
    import io
    import json

    from ruler import judges

    C = corpus.load()
    cs = cases.load("segmentation-10", C, strict=True)
    # 边界用 1-based 段号，与标注同一套坐标（段序列来自那篇文档，不随语料重建而变）
    all_idx = [i for w in cs.cases for i in range(w["lo"] + 1, w["hi"] + 1)]
    must = [m for w in cs.cases for m in w["must"]]

    def show(tag, bounds):
        r = judges.segmentation(C, cs, bounds)
        print(f"  {tag:<14}必切召回 {r['必切召回']:>5}　WD {r['WD']:>5}　误切 {r['误切']:>3}　"
              f"未标注 {r['未标注']:>3}　粒度 {r['粒度']:>7}")
        return r

    if not argv:
        print(f"—— 分段尺子自检 · WindowDiff 窗口 {judges.SEG_K} 段 · {C.line()} ——")
        res = {"全不切": show("全不切（0 刀）", []),
               "全切": show("全切（每段一刀）", all_idx)}
        for k in (3, 5):
            res[f"每{k}"] = show(f"每 {k} 段一刀", all_idx[::k])
        res["手工"] = show("手工（只切 must）", must)
        ok = (res["手工"]["WD"] < res["每3"]["WD"] < res["全切"]["WD"]
              and res["手工"]["误切"] == "0" and res["手工"]["未标注"] == "0"
              and res["全不切"]["必切召回"] == "0%")
        print(f"\n  判据：手工最干净、全切最差、全不切召回 0 ⇒ {'✅ 尺子可用' if ok else '❌ 尺子作废'}")
        return

    files = argv if "--all" not in argv else \
        sorted(glob.glob(os.path.join(corpus.TOOLS, "_bounds_*.json")))
    print(f"—— 分段候选 · {C.line()} ——")
    for f in files:
        if not os.path.exists(f):
            print(f"  {f} 不存在")
            continue
        b = json.load(io.open(f, encoding="utf-8"))
        show(os.path.basename(f)[8:-5], b.get("bounds", b))


def cmd_vocab(argv):
    """**词面缺口**：召不回来的那些题，是不是因为问句与材料"用词对不上"？

    为什么量这个：「词语层指向」那条从 09-17 起挂着状态「存活候选（等触发场景）」，
    触发条件写的是**词面不匹配导致召不回**。而"等"不是一个动作 —— 这个条件是能量的：

      对每道题，算 **问句的字符 bigram 有多大比例出现在靶子块里**（词面覆盖率），
      再看**候选池天花板**（靶子全在池里 = 这题可达）随覆盖率怎么变。

    · 覆盖率低处天花板明显更低 ⇒ **触发条件成立**，该去把「词语层指向」做起来
    · 各处都一样 ⇒ 触发的不是词面，那条该改成有依据的休眠

    用**天花板**而不是最终全中率：天花板不受"装几个名额"影响，量的纯粹是"找不找得到"。
    """
    import collections

    from ruler import retr

    C = corpus.load()
    names = argv if argv else ["single-hop-15", "hard-query-15", "multihop-25", "xdoc-8"]
    buckets = [(0.0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]
    tally = collections.defaultdict(lambda: [0, 0])
    print(f"—— 词面缺口 · {C.line()} ——")
    for nm in names:
        cs = cases.load(nm, C, strict=False)
        picked, pools, _ = retr.vec(C, cs, k=24, cap=24, query_source="raw")
        for c, pool in zip(cs.cases, pools):
            qb = _bigrams(c["q"])
            if not qb:
                continue
            best = 0.0
            for t in c["_targets"]:
                for j in t.group:
                    b = _bigrams(C.body[j])
                    best = max(best, len(qb & b) / len(qb))
            reached = all(any(j in pool for j in t.group) for t in c["_targets"])
            for lo, hi in buckets:
                if lo <= best < hi:
                    tally[(lo, hi)][0] += reached
                    tally[(lo, hi)][1] += 1
                    break
    print(f"\n  {'问句词面覆盖率':>14}{'可达':>12}{'题数':>7}")
    for lo, hi in buckets:
        ok, n = tally[(lo, hi)]
        if n:
            print(f"  {f'{lo:.1f}~{hi:.1f}':>14}{f'{100*ok/n:.0f}%':>12}{n:>7}")
        else:
            print(f"  {f'{lo:.1f}~{hi:.1f}':>14}{'—':>12}{0:>7}")
    print("\n  读法：覆盖率单调地抬可达 ⇒ 词面是闸门（触发成立）；"
          "各档差不多 ⇒ 卡的不是词面，那条改成休眠")


def _bigrams(s):
    import re
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
    elif cmd == "ls":
        cmd_ls()
    elif cmd == "audit":
        sys.exit(1 if cmd_audit() else 0)
    elif cmd == "rebuild":
        cmd_rebuild()
    elif cmd == "migrate":
        cmd_migrate()
    elif cmd == "run":
        cmd_run(rest)
    elif cmd == "gate":
        cmd_gate()
    elif cmd == "seg":
        cmd_seg(rest)
    elif cmd == "vocab":
        cmd_vocab(rest)
    else:
        raise SystemExit(f"不认识：{cmd}\n\n{__doc__}")


if __name__ == "__main__":
    main()
