# -*- coding: utf-8 -*-
"""
**材料臂的对照表** —— 把 `tools/mat-arm.ps1` 跑出来的几份日志并排看。

为什么要单独一个：材料**逐次不同**（规划器每次生成的查询不同），所以
「A 臂比 B 臂多几句」这句话**只有在噪声底已知时才有意义**。
本工具的头两件事就是**把噪声底摆在最上面**：同一配置跑两遍的那两个数。

用法：
    python tools/eval/mat-arm-compare.py off=数据1.log off=数据2.log k5=数据3.log
    （`名字=日志路径`，第一个名字叫 off 的那份当基线；重名 = 同一配置的重复样本）
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# `  aq-g1   grounded    15/8   some   some   ✓`
ROW = re.compile(r"^([a-z]{2}-[a-z0-9]+)\s+(\S+)\s+(\d+)/(\d+)\s")


def load(path):
    raw = io.open(path, "rb").read()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    text = raw.decode(enc, errors="replace")
    out = {}
    for ln in text.splitlines():
        m = ROW.match(ln.strip())
        if m:
            out[m.group(1)] = (int(m.group(3)), int(m.group(4)), m.group(2))
    return out


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    arms = []
    for a in args:
        name, path = a.split("=", 1)
        arms.append((name, path, load(path)))
    base_name, _, base = arms[0]
    ids = sorted(set().union(*[set(d) for _, _, d in arms]))
    if not ids:
        print("！一份都没读出来 —— 检查路径/编码")
        return 1

    print("=" * 78)
    print("每臂：存活句 / 块　（第一个参数当基线）")
    print("=" * 78)

    # ── 一、噪声底：同名两遍 ─────────────────────────────────────────────
    by_name = {}
    for name, path, d in arms:
        by_name.setdefault(name, []).append(d)
    repeats = {k: v for k, v in by_name.items() if len(v) > 1}
    if repeats:
        print("\n一、**噪声底**（同一配置跑两遍 —— 判别的尺子就是它）")
        for name, ds in repeats.items():
            for i in range(1, len(ds)):
                a, b = ds[0], ds[i]
                ks = sorted(set(a) & set(b))
                diff = [k for k in ks if a[k] != b[k]]
                print(f"  {name} vs {name}：句合计 {sum(v[0] for v in a.values())} → "
                      f"{sum(v[0] for v in b.values())} · "
                      f"**逐题不同 {len(diff)}/{len(ks)}**"
                      + (f" （{' '.join(diff[:8])}…）" if diff else ""))
        print("  ⇒ **任何一臂的合计差若落在这个范围内，就是噪声、不是效果**")
    else:
        print("\n一、⚠️ **没有重复样本 ⇒ 噪声底未知** —— 那么这一整张表只能当"
              "「有没有大动作」看，不能当「哪一臂更好」看")

    # ── 二、并排表 ───────────────────────────────────────────────────────
    print("\n二、逐题对照（`·` = 这一臂没采到）")
    hdr = f"  {'题':<8}{'型':<18}" + "".join(f"{n:>12}" for n, _, _ in arms)
    print(hdr)
    for cid in ids:
        cells = ""
        typ = ""
        for name, path, d in arms:
            v = d.get(cid)
            if v is not None and not typ:
                typ = v[2]
            cells += f"{('·' if v is None else f'{v[0]}/{v[1]}'):>12}"
        print(f"  {cid:<8}{typ:<18}{cells}")
    print("  " + "-" * (len(hdr) - 2))
    tot = "".join(f"{sum(v[0] for v in d.values()):>12}" for _, _, d in arms)
    print(f"  {'合计句':<26}{tot}")
    totb = "".join(f"{sum(v[1] for v in d.values()):>12}" for _, _, d in arms)
    print(f"  {'合计块':<26}{totb}")

    # ── 三、相对基线的差 ─────────────────────────────────────────────────
    print("\n三、**相对基线**的差（先看合计，逐题差只能在噪声底之上读）")
    bt = sum(v[0] for v in base.values())
    bb = sum(v[1] for v in base.values())
    for name, path, d in arms[1:]:
        ks = sorted(set(base) & set(d))
        ds = [k for k in ks if base[k][0] != d[k][0]]
        dt = sum(v[0] for v in d.values()) - bt
        db = sum(v[1] for v in d.values()) - bb
        sign = "▲" if dt > 0 else ("▼" if dt < 0 else "＝")
        print(f"  {base_name} → {name}: 句 {bt} → {sum(v[0] for v in d.values())} "
              f"（**{sign}{abs(dt)}**）· 块 {bb} → {sum(v[1] for v in d.values())} "
              f"（{db:+d}）· 逐题不同 {len(ds)}/{len(ks)}")
        need = len(repeats.get(base_name, [])) and len(repeats.get(name, []))
        if not need:
            print("     ⚠️ 这一对**各有几遍？** 少于 2 遍 ⇒ 这个差还分不清是臂还是噪声")
    return 0


if __name__ == "__main__":
    sys.exit(main())
