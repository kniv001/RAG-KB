# -*- coding: utf-8 -*-
"""**`引用对得上` 的假阳性有多少** —— 改判据之前先量分布（项目的老规矩）。

## 为什么要先量

`引用对得上` 的做法是：把带 `[n]` 的子句里的**具体值**（`_VALUE`：数字 + 最多 3 个汉字）
抽出来，要求它**字面**出现在第 n 块里。它是从"2026-09-21 那一批假阳性"里调出来的
（`Region`/`Cset` 那类中英差异、`3层（` 那个全角括号），所以两个动作都有记录在案的理由。

但 2026-09-29 诊断 `aq-p4` 时看到一种**没被覆盖**的：答案写 `1个报文段`，抽出 `1个报文`，
而材料写「先设置 **cwnd=1**，…只发送**一个**报文段」—— 两处错开：

  ① **数字形式**：答案用 `1`、材料用 `一`（同一件事的两种写法）
  ② **三字截断**：`_VALUE` 只取数字后 3 个汉字 ← 那是为了"取值停在标点"（有理由）

⇒ **先数一数这类到底有多少**，再决定要不要动判据、怎么动。
判据是量答案价值的那把尺子，**改它的风险比改管线大**。

## 口径

对每个历史落盘里的每条 `_引用对不上`，把那个值拿回**它引的那几块**去比，分三类：

  · **真缺**：三种写法都不在里面     ⇒ 判据是对的（这是它要抓的）
  · **数字写法**：两边数字↔汉字归一后能找到 ⇒ 假阳性（①）
  · **截断**：把值往后延长到标点能找到    ⇒ 假阳性（②）

用法：python tools/eval/cite-fp-probe.py [bench…]
"""
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from eval import judges                                          # noqa: E402

CN = "一二三四五六七八九"
NUM2CN = {str(i + 1): c for i, c in enumerate(CN)}
CN2NUM = {c: str(i + 1) for i, c in enumerate(CN)}


def num_norm(s):
    """数字归一（只做 1~9 的**单个**数字）：**汉字 → 阿拉伯数字**，单向。

    ⚠️ 第一版把 `1→一` 与 `一→1` **同时**做了（两边互换）—— 于是两边归一完**还是不一样**，
    结果"数字写法"这一类报了 **0 个**，而 `aq-p4` 明明就是这一类。
    **规范形只能往一边倒**：全部映射到阿拉伯数字，汉字形式自然就与数字形式相等了。
    （又一次"仪器没在量我以为它在量的东西"——这一支今天第 N 次。）
    """
    return "".join(CN2NUM.get(ch, ch) for ch in s)


def variants(v):
    """一个值的几种等价写法（**只做有理由的放宽，不是模糊匹配**）。"""
    out = {v, num_norm(v)}
    # 截断：值是 `数字+≤3 汉字`，往后多取几个汉字（材料里的原话常常更长）
    m = re.match(r"^(\d+(?:\.\d+)?)([一-鿿A-Za-z]{0,3})$", v)
    if m:
        for extra in (2, 4, 6):
            out.add(num_norm(m.group(1)) + m.group(2))
            out.add(m.group(1) + m.group(2))
            _ = extra                                     # 见下：延长靠 prefix 匹配
    return out


def classify(value, texts):
    """值在它引的块里怎么找得到 —— 返回 ('真缺'|'数字写法'|'截断'|'找到')。"""
    for t in texts:
        if value in t:
            return "找到"
        if num_norm(value) in num_norm(t):
            return "数字写法"
    # 截断：取**值的前缀**（数字 + 前 2 个汉字）在材料里能继续往后延伸
    m = re.match(r"^(\d+(?:\.\d+)?)([一-鿿A-Za-z]{0,3})$", value)
    if m:
        head = num_norm(m.group(1)) + m.group(2)[:2]
        if head:
            for t in texts:
                if head in num_norm(t):
                    return "截断"
    return "真缺"


def main():
    benches = sys.argv[1:] or ["answer-quality", "news-live"]
    files = []
    for b in benches:
        files += sorted(glob.glob(os.path.join(HERE, "_runs", f"{b}__*.json")))
    files = [f for f in files if not f.endswith(".polluted")]

    tally, examples, n_runs, n_items = {}, {k: [] for k in ("数字写法", "截断", "真缺")}, 0, 0
    for f in files:
        try:
            d = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            continue
        n_runs += 1
        for x in (d.get("results") or []):
            srcs = x.get("sources") or []
            if not srcs or not (x.get("answer") or "").strip():
                continue
            try:
                row = judges.judge(x.get("kind"), x["answer"], srcs, x.get("q") or "",
                                   (x.get("stats") or {}).get("cites"))
            except Exception:
                continue
            for clause, ids, vals in (row.get("_引用对不上") or []):
                for v in vals:
                    n_items += 1
                    texts = []
                    for i in ids:
                        m = re.search(r"^\[(\d+)\]", str(i)) if False else None
                        _ = m
                    # ids 是块号（1 基）⇒ 直接取对应来源的注入文本
                    for i in ids:
                        try:
                            s = srcs[int(i) - 1]
                        except Exception:
                            continue
                        texts.append((s.get("injected") or s.get("_full") or "").replace(" ", "").replace("\n", ""))
                    kind = classify(v, texts)
                    tally[kind] = tally.get(kind, 0) + 1
                    if len(examples[kind]) < 6:
                        examples[kind].append((os.path.basename(f), x["id"], v))

    print(f"扫了 {n_runs} 份落盘，共 {n_items} 个「引用对不上」的值\n")
    print(f"{'类别':<10}{'个数':>6}{'占比':>8}")
    for k in ("真缺", "数字写法", "截断", "找到"):
        n = tally.get(k, 0)
        if n:
            print(f"{k:<10}{n:>6}{100.0 * n / max(1, n_items):>7.1f}%")
    for k in ("数字写法", "截断", "真缺"):
        if examples[k]:
            print(f"\n{k} 的例子：")
            for fn, i, v in examples[k]:
                print(f"  {i:<8}{v:<12}{fn[:44]}")
    print("\n读法：『真缺』那一类是判据**该抓**的；另两类是**假阳性**（同一件事的两种写法/取值截断）。")
    print("      —— 要不要修、怎么修，看这两类占多少、以及**修了会不会把真缺也放过去**。")


if __name__ == "__main__":
    main()
