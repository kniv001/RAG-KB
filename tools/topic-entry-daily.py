# -*- coding: utf-8 -*-
"""
**入口曲线 · 每日一行** —— 把「拿跨议题共享词当入口」这件事挂进轮询，攒几天看它稳不稳。

## 为什么是"每天跑一次"而不是"一次跑完"

2026-09-29 的回放（`tools/topic-entry-replay-probe.py`）只能答**形状**：语料只有 6 天，
曲线在长、最大入口慢慢涨到 18（没串），但**「新增入口/新增议题」在 0.06~0.58 之间乱跳
—— 没有平台期**。⇒ **"稳不稳"这 6 个点答不了**，得让真时间流过。

⇒ 本脚本**只做一件事**：每天采一次读数、append 一行到 `data/_entry_curve.csv`。
逻辑全在 `tools/ruler/entry.py`（与回放探针**同一份实现**，免得两处各算各的）。

## ⚠️ 2026-10-06：这份曲线跑了 7 天，**两次都没量到它要量的东西**

看了 09-29~10-05 那 7 行之后加的两道防线 —— 两条都是"**仪器坏了不报错**"那一族：

1. **语料戳列（`feedStamp`）**。那 7 行的 `items/topicsAll/topicsMulti` **一模一样**
   （3376/2164/412）—— **语料从 09-29 22:40 起就没再长过**，所以那不是增长曲线，
   是**同一批语料被重复测了 7 次**。根因不在本脚本：跑材料 A/B 的 `tools/mat-arm.ps1`
   收尾复位时**漏了 `KB_FEED_POLL`/`KB_WEB_ENABLED` 两个开关** ⇒ 重启后的生产**抓取是关的**
   （`/api/feed/stats` 那句「上轮抓取：**还没跑过**」是决定性证据）。
   ⇒ 加上语料戳之后，"同一批语料"在表里**自己就看得出来**了（`--show` 会直接说）。
2. **同输入必须同输出**。那 7 行的 `entries` 在 57/58 之间抖、`biggest` 在 14/15/16 之间抖
   —— 而输入完全相同。根因在 `ruler/entry.py` 的 `hang()`：**并列只按长度排，
   同长度的顺序由 set 的迭代顺序决定，而 `str` 的 hash 每进程随机**（`PYTHONHASHSEED`）。
   已加确定的名字序兜底；修完同一批语料连跑三次完全一致。

⚠️ **修完的数和修之前不可比**（`最大入口 16 → 18`）：并列破平换了，选中的入口词就换了。
⇒ **旧那 7 行要归档、不要拿来当基线**（`data/_entry_curve_frozen_20260929-1005.csv`）。

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

COLS = ["date", "at", "feedStamp", "items", "topicsAll", "topicsMulti", "entries", "biggest",
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
    print(f"{'日期':<12}{'语料戳':>14}{'议题(≥2条)':>11}{'入口':>7}{'最大入口':>9}"
          f"{'覆盖':>7}{'压缩比':>8}{'新增入口':>9}")
    for r in rows:
        st = r.get('feedStamp', '—')
        print(f"{r['date']:<12}{st:>14}{r['topicsMulti']:>11}{r['entries']:>7}"
              f"{r['biggest']:>9}{float(r['coverage'])*100:>6.0f}%"
              f"{float(r['compression']):>7.2f}:1{r.get('newEntries','—'):>9}")
    st = {r.get('feedStamp') for r in rows}
    if len(rows) > 1 and len(st) == 1:
        print(f"\n⚠️ **所有行的语料戳都是同一个（{list(st)[0]}）** ⇒ 这不是一条增长曲线，"
              f"是**同一批语料被重复测了 {len(rows)} 次**。")
        print("   （2026-09-29~10-05 那份就是这样来的 —— 原因见脚本注释里那条。）")
    else:
        print(f"\n（{len(st)} 个不同的语料戳 / {len(rows)} 行 ⇒ 语料在长）")
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
            today, time.strftime("%H:%M:%S"), m["feedStamp"], m["items"], m["topicsAll"], m["topicsMulti"],
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
