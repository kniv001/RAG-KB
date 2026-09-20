# -*- coding: utf-8 -*-
"""
**答案侧判据** —— 全部是机械判据，不请裁判模型。

为什么不上裁判模型：台账里量过两次 —— 换模型当第二标注者，同意率 **53%**（llama3.1:8b 是 73%）；
「读概率」当判断器在三个真实任务上全败。**裁判本身不可靠时，它给的分没有意义。**

而答案的**格式契约**写得足够硬，机械判据就能覆盖大半（`ANSWER_SYSTEM` 的三段式）：
  【甲】与知识库无关（闲聊/身份/指代）→ **不许**提「知识库中没有」
  【乙】有资料 → 严格依据资料，引用处用 `[编号]`
  【丙】没资料 → ① 先声明知识库没有 ② 再用通用知识答 ③ 标注「以下为通用知识」

⇒ 判据：引用编号有效 / 有引用 / 该声明时声明 / 该标注时标注 / 不该声明时没声明 / 数字落地。
最后一条复用摘要那条线已有的机械判据（值特异的解码缺陷提示词治不了，见 digit-survive-probe）。
"""
import re

# 「知识库没有」的各种说法 —— 声明缺失
_MISSING = re.compile(
    r"知识库(中|里)?(并)?(没有|不含|未收录)|资料(中|里)?没有|没有(相关|这方[面向]的)?(资料|信息)"
    r"|知识库(中|里)?未(找到|收录)|未找到相关")
# 「以下是通用知识」的标注
_GENERAL = re.compile(
    r"通用知识|未引用(你的)?知识库|不(来自|是来自)(你的)?(个人)?知识库|非来自.{0,6}知识库")
_CITE = re.compile(r"\[(\d{1,2})\]")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def cites(answer):
    """答案里出现的引用编号（去重、保序）。"""
    seen, out = set(), []
    for m in _CITE.finditer(answer or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def cites_valid(answer, n_sources):
    """编号是否都落在**这次真的召回到的来源**范围内 —— 编造编号是硬伤。"""
    cs = cites(answer)
    if not cs:
        return None                      # 没引用，由 cites_any 单独判
    return all(1 <= n <= n_sources for n in cs)


def has_cite(answer):
    return bool(cites(answer))


# 系统提示里的分类标签被抄进正文 —— 用户会直接看到 `【乙】知识性问题，且【参考资料】里有内容`
# 这种东西。这是纯格式缺陷，与题型无关。（同类缺陷在别的提示词上也踩过：给了示例值就会被抄。）
_LABEL = re.compile(r"^\s*【[甲乙丙]】|^\s*【(知识性|与知识库)")


def label_leak(answer):
    return bool(_LABEL.search(answer or ""))


def declares_missing(answer):
    return bool(_MISSING.search(answer or ""))


def marks_general(answer):
    return bool(_GENERAL.search(answer or ""))


def numbers_grounded(answer, context):
    """答案里的数字能不能在（问题 + 召回资料）里找到。

    找得到的口径与摘要那条一致：**逐字**在上下文里出现。
    返回 (落地数, 总数, 没落地的那些)。**不当硬性 pass/fail** —— 它会有正当例外
    （如「三个方向」这种模型自己数的），所以只报比例，让人看得见异常。
    """
    ctx = _norm(context)
    nums = [n for n in _NUM.findall(_norm(answer)) if len(n) >= 2]   # 单位数太容易碰巧
    if not nums:
        return 0, 0, []
    bad = [n for n in nums if n not in ctx]
    return len(nums) - len(bad), len(nums), bad


# ── 按题型给判据 ────────────────────────────────────────────────────────
def judge(kind, answer, sources, question):
    """返回 {判据名: True/False/None}，None = 该题不适用这一条。"""
    ctx = (question or "") + "\n" + "\n".join(s.get("preview", "") for s in sources)
    r = {}
    grounded, total, bad = numbers_grounded(answer, ctx)
    r["数字落地"] = f"{grounded}/{total}" if total else "—"
    r["_数字未落地"] = bad[:6]
    r["无标签泄漏"] = not label_leak(answer)

    if kind == "grounded":                      # 【乙】有资料
        r["有引用"] = has_cite(answer)
        r["引用有效"] = cites_valid(answer, len(sources))
        # 【乙】的契约是「不得编造资料里没有的内容」。数字**逐字**落地率过低
        # ⇒ 基本可以断定把通用知识掺进了有据的回答（实测那道 AOF 题：来源只说了
        # 「最多丢失 1 秒」，答案却写出「0 秒」「几十秒」——1/6）。
        # 阈值取 0.5：模型自己*推导*出的个别值（如「不丢失」写成「0 秒」）不该误伤，
        # 但 1/6 这种断崖要挂。
        r["数字有据"] = None if not total else (grounded / total >= 0.5)
    elif kind == "ungrounded":                  # 【丙】没资料
        r["声明了没有"] = declares_missing(answer)
        r["标注了通用知识"] = marks_general(answer)
    elif kind == "chitchat":                    # 【甲】与知识库无关
        r["没误报「知识库没有」"] = not declares_missing(answer)
    return r


def passes(row, kind):
    """这题过没过。

    **None = 不适用，要跳过**（例如「数字有据」在没有数字的题上不适用）。
    第一版写成 `all(row.get(k) for k in need)` —— `None` 是假值，于是把两道教条
    答得完全正确的题判成了失败（节点亲和那道整篇没有数字）。判据自己也会错。
    """
    return all(row.get(k) for k in PASS[kind] if row.get(k) is not None)


# 每类题的**通过条件**（哪些判据必须为真）
PASS = {
    "grounded": ["有引用", "引用有效", "数字有据", "无标签泄漏"],
    "ungrounded": ["声明了没有", "标注了通用知识", "无标签泄漏"],
    "chitchat": ["没误报「知识库没有」", "无标签泄漏"],
}
