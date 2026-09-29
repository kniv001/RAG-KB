# -*- coding: utf-8 -*-
"""
**入口曲线 · 每日一行** —— 把「拿跨议题共享词当入口」这件事挂进轮询，攒几天看它稳不稳。

## 为什么是"每天跑一次"而不是"一次跑完"

2026-09-29 的回放（`tools/topic-entry-replay-probe.py`）只能答**形状**：语料只有 6 天，
曲线在长、最大入口慢慢涨到 18（没串），但**「新增入口/新增议题」在 0.06~0.58 之间乱跳
—— 没有平台期**。⇒ **"稳不稳"这 6 个点答不了**，得让真时间流过。

⇒ 本脚本**只做一件事**：每天采一次读数、append 一行到 `data/_entry_curve.csv`。
逻辑全在 `tools/ruler/entry.py`（与回放探针**同一份实现**，免得两处各算各的）。

## 它不碰任何生产东西

· **只读**：只 `SELECT`，不改库、不调模型、不需要应用在跑（只要 psql 连得上）
· **只追加**：CSV 一行；另存一份入口词集合（`_entry_words.json`）用来算"新增入口"
· **同一天跑两次不会写两行**（除 `--force`）—— 曲线是一天一个点，重复跑会让它看起来更密

用法：
    python tools/topic-entry-daily.py            # 采一次（今天已采过就跳过）
    python tools/topic-entry-daily.py --force    # 强制再采一行
    python tools/topic-entry-daily.py --show     # 只把已有曲线打出来
"""
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ruler import entry  # noqa: E402

REPO = os.path.dirname(HERE)
CSV = os.path.join(REPO, "data", "_entry_curve.csv")
SNAP = os.path.join(REPO, "data", "_entry_words.json")

COLS = ["date", "at", "items", "topicsAll", "topicsMulti", "entries", "biggest",
        "covered", "coverage", "compression", "newEntries", "newTopics",
        "lo", "hi", "topk"]


def read_rows():
    if not os.path.exists(CSV):
        return []
    with io.open(CSV, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f if l.strip()]
    if len(lines) <= 1:
        return []
    head = lines[0].split(",")
    return [dict(zip(head, l.split(","))) for l in lines[1:]]


def show():
    rows = read_rows()
    if not rows:
        print("（还没有曲线 —— 跑一次 `python tools/topic-entry-daily.py`）")
        return
    print(f"{'日期':<12}{'议题(≥2条)':>11}{'入口':>7}{'最大入口':>9}{'覆盖':>7}"
          f"{'压缩比':>8}{'新增入口':>9}{'新增议题':>9}")
    for r in rows:
        print(f"{r['date']:<12}{r['topicsMulti']:>11}{r['entries']:>7}{r['biggest']:>9}"
              f"{float(r['coverage'])*100:>6.0f}%{float(r['compression']):>7.2f}:1"
              f"{r.get('newEntries','—'):>9}{r.get('newTopics','—'):>9}")
    print(f"\n（曲线在 {os.path.relpath(CSV, REPO)}；一天一个点，"
          f"**看的是入口的增长是不是明显慢于议题、以及有没有平台期**）")


def main():
    if "--show" in sys.argv:
        show()
        return
    force = "--force" in sys.argv
    today = time.strftime("%Y-%m-%d")
    rows = read_rows()
    if rows and rows[-1]["date"] == today and not force:
        print(f"今天（{today}）已经采过了 —— 加 --force 可以再采一行；--show 看曲线。")
        return

    m = entry.measure()
    prev_words = set()
    if os.path.exists(SNAP):
        try:
            prev_words = set(json.load(io.open(SNAP, encoding="utf-8")).get("entryWords", []))
        except Exception:
            prev_words = set()
    cur = set(m["entryWords"])
    new_e = len(cur - prev_words) if prev_words else ""
    # "新增议题" = 与上一行比（≥2 条的议题数）
    new_t = (m["topicsMulti"] - int(rows[-1]["topicsMulti"])) if rows else ""

    first = not os.path.exists(CSV)
    with io.open(CSV, "a", encoding="utf-8") as f:
        if first:
            f.write(",".join(COLS) + "\n")
        f.write(",".join(str(x) for x in [
            today, time.strftime("%H:%M:%S"), m["items"], m["topicsAll"], m["topicsMulti"],
            m["entries"], m["biggest"], m["covered"], f"{m['coverage']:.4f}",
            f"{m['compression']:.3f}", new_e, new_t, m["lo"], m["hi"], m["topk"]]) + "\n")
    json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "entryWords": sorted(cur)},
              io.open(SNAP, "w", encoding="utf-8"), ensure_ascii=False)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 入口曲线 +1 行：")
    print(f"    条目 {m['items']} · 议题 {m['topicsAll']}（≥2 条 {m['topicsMulti']}）"
          f" → **入口 {m['entries']}** · 最大挂 {m['biggest']} · "
          f"覆盖 {m['coverage']*100:.0f}% · 压缩 {m['compression']:.2f}:1")
    if new_e != "":
        print(f"    新增入口 {new_e} · 新增议题 {new_t}")
    print(f"    → {os.path.relpath(CSV, REPO)}")


if __name__ == "__main__":
    main()
