# -*- coding: utf-8 -*-
"""
**「部分冻结」能不能机械检测** —— 拿已知答案的场景量代理量的成色。

背景（台账 2026-09-20）：摘要合并有一类退化是**冻结** —— 新信息一条不落地，
而条目数一条不差，所有已有判据（丢条目 / 条目变少）都看不见。
当时加了检测，但只抓得住**完全冻结**（输出与输入**逐字相同**）。
残余是**部分冻结**：改了几条、漏了几条，输出与输入不同 ⇒ 检测沉默。

**难在哪**：要判"漏了"，得先知道"哪几条本该变" —— 而那是**语义链接**
（材料里哪句话是要改哪条）。运行时没有这个。

**但"没有"是个可以实测的结论，不该坐着推。** 这个探针就是来测的：

  · 复用 `summary-loss-probe.py` 的种子事实与 5 轮对话（那里**逐轮写死了
    `UPDATES`「必须落地的字面量」**，也就是 ground truth）
  · 每轮再算几个**只用材料就能算**的代理量（不看 UPDATES）
  · 最后看代理量与 ground truth 的**列联表** —— 抓得住多少、误报多少

当前代理量（都可机械算，不需要知道"本该改哪条"）：
  **P2「材料里没被吸收的数字」** = 出现在本轮对话、却在本轮摘要里**找不到**的数字。
     选它是因为它正是 `SummaryService.warnIfNumberCorrupted` 已经在用的形状
     （摘要里的数字必须能在输入里找到）—— **反过来用一遍**。

用法：python tools/summary-freeze-probe.py [链数，默认 3]
"""
import importlib.util
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 复用已有的场景与合并实现，**不复制**（复制会分叉，两边的结论就不再可比）
_spec = importlib.util.spec_from_file_location(
    "slp", os.path.join(HERE, "summary-loss-probe.py"))
slp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slp)

NUM = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(s):
    return {n for n in NUM.findall(s or "")}


def p2_undigested(fresh, best_items):
    """**材料里出现、摘要里找不到**的数字。不看 ground truth。"""
    in_best = numbers_in(" ".join(best_items))
    return sorted(numbers_in(fresh) - in_best)


def p1_novel(old_items, best_items):
    """新条目数：在旧列表里找不到着落的条目（用探针自己的 counterpart 规则）。"""
    return len([it for it in best_items
                if not any(slp.counterpart(it, o) for o in old_items)])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    chains = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    arm = os.environ.get("KB_FREEZE_ARM", "A")

    # **垫到 18 条**：那正是当时量出冻结的条件（MAX_ITEMS=20）
    pad = int(os.environ.get("KB_SEED_PAD", "12"))
    seed = slp.SEED + ("\n" + "\n".join(slp.PAD[:pad]) if pad else "")
    print(f"初始 {len([x for x in seed.splitlines() if x.strip()])} 条　臂 {arm}　"
          f"{chains} 条链 × {len(slp.ROUNDS)} 轮\n")

    rows = []          # (轮, 落地?, P2 命中的数字, P1 新条目数, 条目数)
    for c in range(chains):
        old = seed
        for i, rnd in enumerate(slp.ROUNDS):
            joined, _, _ = slp.merge(old, rnd, arm)
            items = [x.strip() for x in (joined or "").split("\n") if x.strip()]
            if not items:
                continue
            text = " ".join(items)
            landed = any(u in text for u in slp.UPDATES[i])      # ground truth
            p2 = p2_undigested(rnd, items)
            p1 = p1_novel([x.strip() for x in old.split("\n") if x.strip()], items)
            rows.append((c + 1, i + 1, landed, p2, p1, len(items)))
            print(f"  链{c+1} 轮{i+1}　{'✅落地' if landed else '❌没落地'}"
                  f"　P2 未吸收数字={p2}　P1 新条目={p1}　条目数={len(items)}", flush=True)
            old = "\n".join(items)

    # ---- 列联表：P2 报「有未吸收的数字」× 实际落地 ----
    tp = sum(1 for r in rows if r[3] and not r[2])      # 报了，且确实没落地
    fp = sum(1 for r in rows if r[3] and r[2])          # 报了，但其实落地了
    fn = sum(1 for r in rows if not r[3] and not r[2])  # 没报，但确实没落地（漏报）
    tn = sum(1 for r in rows if not r[3] and r[2])
    print(f"\n—— 代理量 P2「材料里没被吸收的数字」 vs 真实落地（n={len(rows)}）——")
    print(f"{'':16}{'真没落地':>10}{'真落地':>10}")
    print(f"{'P2 报有':16}{tp:>10}{fp:>10}   ← 误报率 {fp/(tp+fp) if tp+fp else 0:.0%}")
    print(f"{'P2 没报':16}{fn:>10}{tn:>10}   ← 漏报率 {fn/(fn+tn) if fn+tn else 0:.0%}")

    # **分数字型与非数字型更新** —— 这个代理量的盲区应当在这里现形
    num_rounds = [i for i, u in enumerate(slp.UPDATES) if any(NUM.search(x) for x in u)]
    print(f"\n（注意：UPDATES 里只有第 {[i+1 for i in num_rounds]} 轮含数字，"
          f"其余轮次的更新是**词**不是数 ⇒ P2 天然看不见它们。"
          f"这是这个代理量的结构性盲区，不是调参能解决的。）")


if __name__ == "__main__":
    main()
