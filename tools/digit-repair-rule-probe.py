# -*- coding: utf-8 -*-
"""
数字修复规则的**误伤面**：修复本身不敢启用，怕的是把正常的时间/比例改坏。
这条探针不调模型 —— 规则是纯字符串函数，验的是它**会不会动不该动的**。

规则（与 SummaryService 的判据同源）：
  形态 `(\\d{2,})\\s*[:：](?!\\d)`，且
    ① 这个数字**不在**输入里出现，且
    ② 输入里存在一个**以它为前缀的更长数字**
  → 判定末位被冒号顶掉，替换成那个更长的数字。
  形态 `60: 600`（模型自己补回来了）→ 整段并成一个 `600`，不留下重复。

用法：python tools/digit-repair-rule-probe.py
"""
import re
import sys

DIGIT_COLON = re.compile(r"(\d{2,})\s*[:：](?!\d)")
ANY_NUMBER = re.compile(r"\d+")


def repair(items, source):
    src = set(ANY_NUMBER.findall(source))
    longer = {}
    for s in src:
        for k in range(2, len(s)):
            longer.setdefault(s[:k], s)
    out = []
    for it in items:
        fixed = it

        # 先处理「60: 600」这种自己补回来的：整段并成一个
        def join_dup(m):
            d, sep, rest = m.group(1), m.group(2), m.group(3)
            tgt = longer.get(d)
            if tgt and rest == tgt:
                return tgt
            return m.group(0)

        fixed = re.sub(r"(\d{2,})(\s*[:：]\s*)(\d{2,})", join_dup, fixed)

        def one(m):
            d = m.group(1)
            if d in src:
                return m.group(0)          # 输入里本来就有「300:」这种写法
            tgt = longer.get(d)
            return tgt if tgt else m.group(0)

        fixed = DIGIT_COLON.sub(one, fixed)
        out.append(fixed)
    return out


CASES = [
    # (说明, 条目, 输入, 期望)
    ("糊掉的 600（末位被顶掉）", ["分块粒度：— → 60: 字"], "块大小定为 600 字，改成 450",
     ["分块粒度：— → 600 字"]),
    ("糊掉的 3000", ["上限：— → 300:"], "上限 3000 条", ["上限：— → 3000"]),
    ("糊掉的 24576（全角冒号）", ["窗口：— → 2457："], "窗口 24576", ["窗口：— → 24576"]),
    ("自己补回来的「60: 600」", ["分块粒度：— → 60: 600 字"], "块大小定为 600 字",
     ["分块粒度：— → 600 字"]),
    ("正常时间 12:30 不能动", ["会议：— → 12:30 开始"], "会议 12:30 开始",
     ["会议：— → 12:30 开始"]),
    ("正常比例 3:1 不能动", ["压缩比：— → 3:1"], "压缩比 3:1",
     ["压缩比：— → 3:1"]),
    ("正常比例 16:9 不能动", ["画幅：— → 16:9"], "画幅 16:9", ["画幅：— → 16:9"]),
    ("输入里本来就有「300:」这种写法", ["格式：— → 300: 开头"], "格式 300: 开头",
     ["格式：— → 300: 开头"]),
    ("数字干净时一个字不动", ["窗口：— → 24576"], "窗口 24576 已改", ["窗口：— → 24576"]),
    ("多个中招一起修", ["粒度 60: 字，上限 300:"], "粒度 600 字，上限 3000",
     ["粒度 600 字，上限 3000"]),
    ("旧值也在输入里（600→450 的变化式）", ["粒度：600 字 → 45: 字"], "粒度 600 字改成 450 字",
     ["粒度：600 字 → 450 字"]),
    ("冒号后紧跟数字的时间戳 10:45:12", ["时间：— → 10:45:12"], "时间 10:45:12",
     ["时间：— → 10:45:12"]),
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    bad = 0
    for name, items, source, want in CASES:
        got = repair(items, source)
        ok = got == want
        bad += not ok
        print(f"{'✅' if ok else '❌'} {name}")
        if not ok:
            print(f"     期望 {want}")
            print(f"     实得 {got}")
    print(f"\n{len(CASES)-bad}/{len(CASES)} 通过"
          f"（含 {sum(1 for c in CASES if '不能动' in c[0] or '时间戳' in c[0])} 条误伤用例）")


if __name__ == "__main__":
    main()
