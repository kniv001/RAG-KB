# -*- coding: utf-8 -*-
"""
**句子级切分** —— 索引与"地址展开"共用同一份实现，**带字符区间**。

## 为什么不直接用分析脚本里那个正则

`think-structure-read.py` / `-eval.py` 用的是 `re.split(r"[。！？；\n]+")` ——
那个是给**相似度比对**用的，大致切对就行。而索引里的句子要**被代码原样拼进正文**
（地址计划 → 展开），所以要求不一样：

  · 必须返回 **char_start / char_end**（半开区间），且**逐字忠实于原文**
    （不丢标点、不改空白）—— 靠"再切一遍"是不行的，两次切法一旦漂移就错位
  · 三类文本**不能按句末标点切**：
      - **代码围栏**（```…```）：里面全是 `;`、中文注释，切了就不是代码了
      - **表格**（`|…|`）：一行是一条记录，切在行中间会毁掉对齐
      - **标题**（`# …`）：本身就是一个单元
  · **列表项**（`- ` / `1. `）每项自成一句 —— 跨项合并会把两件事粘一起

## 一份实现，两处用

    from sentence_split import split
    for start, end, kind, text in split(chunk_text): ...

索引构建（`build-sentences.py`）与将来的地址展开都从这里拿切片，
**不各写一份** —— 本项目在"同一个判据两份实现"上栽过。

## 切分是判断，不是事实

`MIN_LEN` 那个合并阈值、以及"表格整行不切"都是**选择**。它们改了，索引就变了，
所以每一行都带 `stamp`（语料戳）—— 换切法要重建，且**旧的分句数字不可与新数字直接比**。
"""
import re

# 太短的片段（"所以"、"但是"、"第一，"）向下并进下一句 ——
# 单独成句既没有信息量、又让地址变碎（模型要多引用几个号）
MIN_LEN = 12

# 句末标点。**逗号不断句** —— 中文技术文档里逗号密度极高，切了会碎成短语。
_SENT_END = "。！？；"
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEAD = re.compile(r"^\s*#{1,6}\s")


def split(text):
    """→ [(char_start, char_end, kind, text)]，kind ∈ {sent, table, code, head}。

    区间是**原文里的半开区间**；`text[start:end]` 必然等于返回的 `text` 字段
    （这条是自检的：`build-sentences.py` 会逐句核一遍再入库）。
    """
    if not text:
        return []
    lines, pos = [], 0
    for ln in text.split("\n"):
        lines.append((pos, pos + len(ln), ln))
        pos += len(ln) + 1          # +1：被 split 吃掉的换行

    units, i = [], 0
    while i < len(lines):
        st, en, ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue

        # ① 代码围栏：整块一个单元（含首尾围栏行）
        m = _FENCE.match(ln)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < len(lines) and not lines[j][2].strip().startswith(fence):
                j += 1
            end = lines[j][1] if j < len(lines) else lines[-1][1]
            units.append((st, end, "code", text[st:end]))
            i = j + 1
            continue

        # ② 表格：连续的表行，**每行一个单元**
        if s.startswith("|"):
            j = i
            while j < len(lines) and lines[j][2].strip().startswith("|"):
                a, b, l2 = lines[j]
                if l2.strip():
                    units.append((a, b, "table", l2))
                j += 1
            i = j
            continue

        # ③ 标题
        if _HEAD.match(ln):
            units.append((st, en, "head", ln))
            i += 1
            continue

        # ④ 普通行（含列表项）：按句末标点切
        for mm in re.finditer(rf"[^{_SENT_END}]+[{_SENT_END}]*", ln):
            seg = mm.group(0)
            if seg.strip():
                units.append((st + mm.start(), st + mm.end(), "sent", seg))
        i += 1

    return _merge(units)


def _merge(units):
    """把过短的 **sent** 并进下一句（只并 sent，表格/代码/标题一根汗毛都不动）。

    ⚠️ **只并同一行内相邻的碎片**（`上.end == 下.start`）。这条是自检逼出来的：
    第一版允许跨行合并，于是 `- 第一项：增量回收` 与 `- 第二项：…` 被并成一句 ——
    ① 语义上把两件事粘一起 ② **区间对不回原文**（中间那个换行没算进去），
    而"区间必须能逐字还原"正是这个模块存在的理由。
    """
    out = []
    for u in units:
        if (out and u[2] == "sent" and out[-1][2] == "sent"
                and out[-1][1] == u[0]                     # ← 相邻，中间没有缝
                and len(out[-1][3].strip()) < MIN_LEN):
            p = out.pop()
            out.append((p[0], u[1], "sent", p[3] + u[3]))
        else:
            out.append(u)
    return out


def sentence_of(text, offset):
    """给定正文里的字符位置，返回它落在哪一句 —— 排查/对照用。"""
    for a, b, k, t in split(text):
        if a <= offset < b:
            return (a, b, k, t)
    return None
