# -*- coding: utf-8 -*-
"""
**原子保护区**：先把「数字 / 引用 / 公式 / 链接 / 行内代码 / 引号术语」标出来，
切分时**禁止切进去** —— 然后拿今天的分段尺子量。

想法（用户提的）：模型在**局部**切分上很强，那就把"不该被切开"的东西**提前识别成原子**，
后面的切分只在原子**之外**动。这与上游清单里的"围栏感知（代码块当原子）"同源，
但要**扩类**：今天量到的现状是 **96 个块边界里 37 个切在代码/日志中间**，
而此前那条"代码后置"规则（中文占比 < 0.25）只是它的**粗糙版**。

**不用模型**：保护区用字面规则标，评估用 `seg-ruler.py` 的 WD。
三种切分各跑一遍「加保护 vs 不加保护」。

用法：python tools/atomic-guard-probe.py
"""
import importlib.util
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEGS = os.path.join(HERE, "..", "data", "_bintree-segs.json")

# 六类"原子"的字面特征（都可机械识别）
UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:字|秒|毫秒|分钟|小时|天|次|条|个|台|块|MB|GB|KB|bit|r/s|%|倍)"  # 数字+单位
    r"|\[\d+\]|【\d+】|[①-⑩]"                                                            # 引用
    r"|\$[^$]{2,}\$|\\[a-zA-Z]+\{[^}]{0,40}\}"                                            # 公式
    r"|https?://\S+|\b\w+\.(?:com|cn|org|io)\b"                                            # 链接
    r"|`[^`]{2,40}`"                                                                       # 行内代码
    r"|「[^」]{2,30}」|“[^”]{2,30}”"                                                        # 引号术语
)


def crosses(a, b, w=30):
    """接缝两侧是否**切在一个原子内部**（把接缝前后的文本拼起来看有没有跨缝的匹配）"""
    ja, jb = a[-w:], b[:w]
    joined = ja + jb
    for m in UNIT.finditer(joined):
        if m.start() < len(ja) < m.end():
            return True
    return False


def legalize(segs, bounds, max_shift=3):
    """把"切在原子内部"的边界挪到最近的不切原子的位置"""
    out, prev = [], 0
    for b in bounds:
        nb = None
        for d in range(0, max_shift + 1):
            for cand in (b - d, b + d):
                if cand <= prev or cand >= len(segs):
                    continue
                if not crosses(segs[cand - 2], segs[cand - 1]):
                    nb = cand
                    break
            if nb:
                break
        nb = nb or b
        out.append(nb); prev = nb
    return out


def count_bad(segs, bounds):
    return sum(1 for b in bounds if 1 < b < len(segs) and crosses(segs[b - 2], segs[b - 1]))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    spec = importlib.util.spec_from_file_location("sr", os.path.join(HERE, "seg-ruler.py"))
    sr = importlib.util.module_from_spec(spec); spec.loader.exec_module(sr)
    ann = sr.load()
    segs = json.load(io.open(SEGS, encoding="utf-8"))["segs"]

    # 语料里到底有多少"原子"、有多少接缝切在原子内部
    total_units = sum(len(UNIT.findall(s)) for s in segs)
    adj = sum(1 for i in range(1, len(segs)) if crosses(segs[i - 1], segs[i]))
    print(f"语料 {len(segs)} 段：识别到原子 {total_units} 处；"
          f"**{adj} 个相邻接缝切在原子内部**\n")

    sets = {}
    sets["现状分块"] = json.load(io.open(os.path.join(HERE, "_bounds_chunking.json"),
                                       encoding="utf-8"))["bounds"]
    sets["指认式+代码后置"] = json.load(io.open(os.path.join(HERE, "_bounds_point_f25.json"),
                                           encoding="utf-8"))["bounds"]

    print(f"{'切分':<18}{'切原子内':>10}{'加保护后':>10}{'WD(原)':>9}{'WD(加保护)':>12}")
    for name, b in sets.items():
        bad0 = count_bad(segs, b)
        fixed = legalize(segs, b)
        bad1 = count_bad(segs, fixed)
        r0 = sr.score(b, ann); r1 = sr.score(fixed, ann)
        print(f"{name:<18}{bad0:>10}{bad1:>10}{r0['WD']:>9.2f}{r1['WD']:>12.2f}")

    # 免费基线也过一遍（每 k 段）
    print()
    all_idx = [i for w in ann["windows"] for i in range(w["lo"] + 1, w["hi"] + 1)]
    for k in (3, 5, 8):
        b = all_idx[::k]
        fixed = legalize(segs, b)
        print(f"{'每 '+str(k)+' 段':<18}{count_bad(segs,b):>10}{count_bad(segs,fixed):>10}"
              f"{sr.score(b,ann)['WD']:>9.2f}{sr.score(fixed,ann)['WD']:>12.2f}")
    print("\n（加保护只动边界位置、不增删切点 ⇒ 若 WD 下降，说明「别切进原子」这件事本身有价值）")


if __name__ == "__main__":
    main()
