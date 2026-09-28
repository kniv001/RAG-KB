# -*- coding: utf-8 -*-
"""**基准的第三张网**：拿**生产那条路**去量每道题"还剩多少材料"，与题面声明的前提对比。

## 三张网各管一段（互为盲区，都要跑）

| 网 | 工具 | 靠什么 | 不需要什么 | 盲区 |
|---|---|---|---|---|
| ① 声明式 | `eval.py audit` | 题面写的 `must_find` / `breaks_if_found` 摘录 | 模型、应用 | **换词**（语料用别的说法写进了答案） |
| ② 行为式 | `bench-rot-probe.py` | 模型"声明没有"的比例 | 应用 | 要**跑过**才有读数；只是**指示器**不是判决 |
| **③ 材料式** | **本工具** | **生产那条路**给这道题剩几句材料 | **模型**（9 秒/题、不等生成） | 仍要**应用在跑**；判"是不是答案"还是要人 |

③ 的价值在于它量的**正是那个前提本身**、且**用的就是生产线**（不是复刻）：
`grounded-partial` / `ungrounded` 的定义就是"库里没有那个东西"，
而生产里"有没有"的**操作性定义**是那条日志 —— `材料地板 0.65（…）：过滤后剩 N 句 / M 块`。

## 前提怎么写

    "premise": {"material": "none"}     # 生产上应当**一句都活不下来**（ungrounded）
    "premise": {"material": "some"}     # 应当**还有材料**（grounded / grounded-partial）

不写就不查这道题（照跑）。

## 用法

    python tools/eval/bench-material-probe.py answer-quality            # 量 + 对表
    python tools/eval/bench-material-probe.py answer-quality --record    # 把"现状"写成前提草稿
"""
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
LOG = os.path.join(REPO, "data", "app.log")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FLOOR_RE = re.compile(r"材料地板 ([\d.]+)（(.{0,20})）：过滤后剩 (\d+) 句 / (\d+) 块")
# 题型 → 它默认该有的材料状态（写进 premise 的默认值；题面写了就以题面为准）
DEFAULT = {
    "grounded": "some",
    "grounded-partial": "some",
    "ungrounded": "none",
}


def read_floor_since(t0):
    """按**行首时间戳**读（不按文件偏移）—— `app.log` 10MB 就轮转，
    偏移法会只读到后半段，症状是"采到的题数比题集少"、**看起来像探针没发出去**。"""
    import glob
    import gzip
    out = {}
    for path in sorted(glob.glob(LOG + "*")):
        op = gzip.open if path.endswith(".gz") else io.open
        try:
            with op(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = FLOOR_RE.search(line)
                    if not m:
                        continue
                    ts = line[:19]
                    if ts >= t0:
                        out[m.group(2)] = (int(m.group(3)), int(m.group(4)))
        except Exception as e:                                   # noqa: BLE001
            print(f"  ！（{os.path.basename(path)} 读不动：{e}）")
    return out


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "answer-quality"
    record = "--record" in sys.argv
    bench = json.load(io.open(os.path.join(HERE, "benches", name + ".json"), encoding="utf-8"))
    cases = {c["id"]: c for c in bench["cases"]}

    subprocess.run(["node", os.path.join(TOOLS, "clear-answers.mjs")],
                   cwd=REPO, check=False, stdout=subprocess.DEVNULL)
    t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"采材料：{name} {len(cases)} 题（不等生成）……", flush=True)
    subprocess.run(["node", os.path.join(HERE, "material-probe.mjs"),
                    "--bench", name, "--model", "qwen3:4b", "--wait", "9000"], cwd=REPO)
    got = read_floor_since(t0)

    keyed = {}
    for cid, c in cases.items():
        pre = (c.get("q") or "")[:20]
        keyed[cid] = got.get(pre)

    print(f"\n===== {name}：每题的**材料** vs 题面声明 =====")
    print(f"{'题':<8}{'题型':<18}{'剩句/块':>10}{'声明':>7}{'现状':>7}   ")
    bad = miss = noprem = 0
    for cid, c in cases.items():
        v = keyed[cid]
        if v is None:
            miss += 1
            continue
        cur = "none" if v[0] == 0 else "some"
        want = ((c.get("premise") or {}).get("material")
                or DEFAULT.get(c.get("kind"), ""))
        if not want:
            noprem += 1
            mark = "（这一型不该用材料判）"
        elif want != cur:
            bad += 1
            mark = f"✗ **与声明不符**（声明 {want}、实测 {cur}）"
        else:
            mark = "✓"
        if c.get("premise", {}).get("material") is None and not record:
            mark += "　（前提没写，按题型默认判）"
        print(f"{cid:<8}{c.get('kind',''):<18}{str(v[0]) + '/' + str(v[1]):>10}{want or '—':>7}{cur:>7}   {mark}")
    if record:
        # ⚠️ **写的是"题型的主张"，不是实测现状**（第一版写反了）。
        # 把实测存成前提 = **把发现抹掉** —— 那 4 处不符会永远变成 ✓，
        # 而它们恰恰是这份工具存在的理由（`aq-p2/p6` 行为上已经不是 grounded-partial 了）。
        # 前提写下**它声称的东西**，工具才有资格说"它破了"。
        # 真有题目**明知偏离而要接受**，就在题面上手写 `premise.material` + 一句 `note`。
        for cid, c in cases.items():
            if c.get("kind") not in DEFAULT:
                continue
            c.setdefault("premise", {})["material"] = DEFAULT[c["kind"]]
        p = os.path.join(HERE, "benches", name + ".json")
        io.open(p, "w", encoding="utf-8").write(
            json.dumps(bench, ensure_ascii=False, indent=1) + "\n")
        print(f"\n（已把**题型主张**写进 {os.path.relpath(p, REPO)} 的 premise.material ——"
              f" 不是实测值；上面那几处 ✗ 就是它要替你记住的不符）")
    print(f"\n不符 {bad} 题 · 没采到 {miss} 题 · 无默认前提 {noprem} 题")
    if miss:
        print("⚠ **没采到** 与「实测为 none」是两件事 —— 先查探针，别当读数用。")


if __name__ == "__main__":
    main()
