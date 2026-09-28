# -*- coding: utf-8 -*-
"""**从日志回读材料 A/B**（按 PID 分组，**不按文件偏移**）。

为什么单独一个读法：
  · **日志 10MB 就轮转**（本机开着 DEBUG，实测 ~50 分钟一轮）⇒ 偏移法只读到后半段，
    症状是"采到的题数比题集少"，**看起来像探针没发出去**。时间戳与 PID 跨文件可比。
  · **臂 = 一次应用重启 = 一个 PID** —— 这是最稳的分组键：不用记时间窗、不受轮转影响。
  · 驱动那次崩在"轮转提示"的 `⚠` 上（GBK 控制台装不下）⇒ 驱动**没跑完第三臂**，
    但**日志已经落盘**，所以回读比重跑便宜。

用法：python data/_nb_mat_read.py            # 自动认出日志里所有"实验用的"应用实例
      python data/_nb_mat_read.py 59152,61234 # 指定 PID 与臂名（按顺序 off,nb,nbk）
"""
import glob
import gzip
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
LOG = os.path.join(REPO, "data", "app.log")
BENCH = "answer-quality"

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+\+\d{2}:\d{2}\s+\w+\s+(\d+)\s+---")
FLOOR_RE = re.compile(r"材料地板 ([\d.]+)（(.{0,20})）：过滤后剩 (\d+) 句 / (\d+) 块")
NB_RE = re.compile(r"相邻块补全（±(\d+)）：命中 (\d+) 段 → 新增 (\d+) 段 → 候选 (\d+) 段")
ARMS = ["off", "nb", "nbk"]


def read_all():
    out = []
    for path in sorted(glob.glob(LOG + "*")):
        op = gzip.open if path.endswith(".gz") else io.open
        try:
            with op(path, "rt", encoding="utf-8", errors="replace") as f:
                out += f.readlines()
        except Exception as e:
            print(f"  ！（{os.path.basename(path)} 读不动：{e}）")
    return out


def main():
    lines = read_all()
    print(f"日志共 {len(lines)} 行（含 .gz）")

    # 每次问答都会打两遍「拍平装配」…… 但「材料地板」也是两遍 —— 按 PID 收集，
    # **同一题取最后一条**（那是真正进提示词的那次）。
    per_pid = {}
    for ln in lines:
        m = LINE_RE.match(ln)
        if not m:
            continue
        pid = m.group(2)
        fm = FLOOR_RE.search(ln)
        if fm:
            per_pid.setdefault(pid, {"floor": {}, "nb": [], "first": m.group(1)})
            per_pid[pid]["floor"][fm.group(2)] = (int(fm.group(3)), int(fm.group(4)))
            per_pid[pid]["last"] = m.group(1)
        nm = NB_RE.search(ln)
        if nm and pid in per_pid:
            per_pid[pid]["nb"].append(tuple(int(nm.group(i)) for i in (1, 2, 3, 4)))

    if len(sys.argv) > 1:
        pids = sys.argv[1].split(",")
    else:
        # 实验实例的判据：**采到过 15 题以上地板行**的 PID（日常问答不会这么密）
        pids = [p for p, v in sorted(per_pid.items(), key=lambda x: x[1]["first"])
                if len(v["floor"]) >= 15]
        print(f"认出 {len(pids)} 个实验实例（地板行 ≥15 题）："
              + "、".join(f"{p}（{len(per_pid[p]['floor'])} 题，起 {per_pid[p]['first'][11:]}）"
                          for p in pids))

    bench = json.load(io.open(os.path.join(REPO, "tools", "eval", "benches", BENCH + ".json"),
                              encoding="utf-8"))
    qs = {c["id"]: c["q"] for c in bench["cases"]}

    names = ARMS[:len(pids)]
    print(f"\n===== 逐题材料对照（地板 0.65 过滤后剩 句/块）=====")
    hdr = "".join(f"{n:>12}" for n in names)
    print(f"{'题':<8}{hdr}   读法")
    tot = {n: [0, 0] for n in names}
    gains = {n: 0 for n in names}
    for i, q in qs.items():
        pre = q[:20]
        cells, vals = [], {}
        for n, p in zip(names, pids):
            v = per_pid[p]["floor"].get(pre)
            vals[n] = v
            cells.append(f"{v[0]}/{v[1]}" if v else "—")
            if v:
                tot[n][0] += v[0]
                tot[n][1] += v[1]
        note = ""
        if "off" in vals and vals["off"]:
            for n in names[1:]:
                if vals[n] and vals[n] > vals["off"]:
                    gains[n] += 1
                    note += f"{n}↑ "
                elif vals[n] and vals[n] < vals["off"]:
                    note += f"{n}↓ "
        print(f"{i:<8}" + "".join(f"{c:>12}" for c in cells) + f"   {note}")

    print(f"\n{'合计':<8}" + "".join(f"{tot[n][0]:>7}/{tot[n][1]:<4}" for n in names))
    for n in names[1:]:
        print(f"  材料比 off 多的题：{n} {gains[n]}/{len(qs)}")
    print("\n补邻居读数：")
    for n, p in zip(names, pids):
        v = per_pid[p]["nb"]
        if v:
            h, a, c = (sum(x[i] for x in v) for i in (1, 2, 3))
            print(f"  {n}: {len(v)} 次　命中 {h} 段 → 新增 {a} 段 → 候选 {c} 段"
                  f"（新增 {100.0 * a / max(1, h):.0f}%）")


if __name__ == "__main__":
    main()
