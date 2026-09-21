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
    # **先把引用编号剥掉**：`[12]` 是出处标注，不是"答案里的数字"。
    # 第一版没剥，于是把 `[11]`[12]` 当成了两个数字，判出一堆假失败
    # （实测那道 AOF 题的"未落地数字"就是这个）。
    body = _CITE.sub(" ", answer or "")
    nums = [n for n in _NUM.findall(_norm(body)) if len(n) >= 2]   # 单位数太容易碰巧
    if not nums:
        return 0, 0, []
    bad = [n for n in nums if n not in ctx]
    return len(nums) - len(bad), len(nums), bad


# ── 组织度：答案是在"组织"还是在"复读" ──────────────────────────────────
# 为什么需要它（2026-09-21）：`think:false` 那条路把 decode 从 ~2358 token 压到 ~56，
# 但答案退化成**资料摘抄** —— 而引用有效 / 数字落地 / 无标签泄漏**全都过**，
# 两把尺子在它眼里一片合格。**这个维度此前没有任何仪器。**
#
# 判据是**逐字重合率**：把答案切成 12 字滑窗，看有多大比例能在**完整资料正文**里找到。
#   · 实测标定（同一道题、同一批资料，各 4 次）：
#       分工版（只许写资料里的） **86%**（4/4 全是 86%）
#       不分工版（可自由组织）   **19~44%，中位 21%**
#     ⇒ 分布不重叠，阈值取 **60%**
#   · 仪器自检：拿资料原文当答案 ⇒ 100%；取资料首 150 字 ⇒ 100%（这两条必须成立）
#
# **用完整正文，不能用 preview** —— preview 只覆盖正文的 ~38%，
# 会让重合率被系统性低估（第一版就是这么错的）。
VERBATIM_FLAG = 0.60


def _full_sources(sources):
    """把 sources 里的 docName+seq 回查成**完整块正文**。查不到就退回 preview。"""
    try:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ruler import corpus
        C = corpus.load()
    except Exception:
        return "\n".join((s.get("preview") or "") for s in sources)
    out = []
    for s in sources:
        i = next((k for k in range(C.n)
                  if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
        out.append(C.body[i] if i is not None else (s.get("preview") or ""))
    return "\n".join(out)


def _shingles(s, n=12):
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def verbatim_ratio(answer, sources, n=12):
    """答案逐字来自资料的比例。None = 资料取不到（不适用）。"""
    src = _shingles(_full_sources(sources), n)
    a = _shingles(answer, n)
    if not a or not src:
        return None
    return len(a & src) / len(a)


# ── 照抄长度：**重合率会骗人，连贯段不会** ──────────────────────────────
# 为什么加它（2026-09-21）：实测有一条 5416 字的答案，把提示词+问题+【参考资料】
# 整段回声了回来（最后还把**问题本身**当成"通用知识"列了一条），而它**通过了全部判据**。
# 回头查仪器，发现「逐字重合率」**选错了**：
#
#     基线最差的那条       重合 43%　最长连贯段 **47 字**
#     v3 那条回声          重合 66%　最长连贯段 **551 字**
#     v2 一条无害的短答     重合 64%　最长连贯段   30 字
#
# 重合率是**长度归一化**的，于是把"551 字的整段照抄"和"30 字的术语引用"算成了同一个数
# （66% vs 64%）。**引用一个字段名与照抄一整段，在比率上无法区分。**
# 最长连贯段能干净分开 —— 与「单个占用率数字会误导，要配上另一种量法」是同一条读法。
#
# **标定（这一批 84 条答案，基线是已知良好的对照）**：
#     基线最长 47 字，第二 36，其后 ≤19
#     然后是**从 48 到 142 的空带**（一条都没有）
#     v3 三条落在 143 / 551（另有 84 一条）
# ⇒ 阈值取 **100 字**：坐在一条 3 倍宽的空带中间，基线在它下面有 2.1 倍余量。
#   **不是拟合出来的点**，是空带里的一个位置。
#
# 已知局限（写清楚，别当成没这回事）：这一批里没有"用户就要原文"的题。
# 若将来出现「把那段配置贴出来」这类题，正当的长引用会被这条判挂 —— 届时按题型豁免。
COPY_RUN = 100
# 逐字段的搜索上限。超过它就不必再精确 —— 判据只比 100。
_COPY_CAP = 1000


def longest_copy(answer, sources, lo=12, cap=_COPY_CAP):
    """答案里**最长的一段逐字照抄**（字）。None = 资料取不到（不适用）。

    用二分：长度 n 的照抄段存在 ⟺ 答案与资料的 n 字滑窗集合有交集。
    **不按比例**，所以"引用一个术语"与"照抄一整段"能分开。
    """
    src = _norm(_full_sources(sources))
    a = _norm(answer)
    if not a or not src:
        return None
    if len(a) < lo:
        return 0

    def has(n):
        return any(a[i:i + n] in src for i in range(len(a) - n + 1))

    best, l, h = 0, lo, min(len(a), cap)
    while l <= h:
        mid = (l + h) // 2
        if has(mid):
            best, l = mid, mid + 1
        else:
            h = mid - 1
    return best


# 答案里出现了**提示词脚手架** —— 来源行、或独占一行的段落标记。
# 出现就说明把输入回声出来了，不是"答得不好"而是"根本没答"。
#
# **第一版写错了，被对照抓出来**：原来只匹配 `【参考资料】` 这个**词**，
# 于是把基线 4 条**完全合法**的答案判挂了 —— 它们说的是
# 「（注：以上内容严格依据【参考资料】中布隆过滤器相关文档推导，无外部编造）」。
# **`【参考资料】` 是契约自己的词汇**（`ANSWER_SYSTEM` 就要求模型这么称呼资料），
# 正文里提到它天经地义。**判据烧到了对照身上 ⇒ 错的是判据，不是对照。**
#
# 改法：要的是**结构**不是**词** ——
#   · `_SRC_HEADER`  来源行 `[n] 来源：<文档>（第 k 块）`：应用的渲染格式，正文不该有
#   · `_SCAFFOLD_LINE` 段落标记**独占一行**：回声里 `【问题】`/`【参考资料】` 是行首行尾，
#     而合法正文里的提法都是嵌在句中的（上面那 4 条全是）
_SRC_HEADER = re.compile(r"\[\d{1,2}\]\s*来源：.{0,140}?（第\s*\d+\s*块）")
_SCAFFOLD_LINE = re.compile(r"^[ \t]*【(?:参考资料|问题|知识库主题概览)】[ \t]*$", re.M)


def echoes_prompt(answer):
    """答案里有没有**提示词脚手架**（来源行 / 独占一行的段落标记）。"""
    a = answer or ""
    return bool(_SRC_HEADER.search(a) or _SCAFFOLD_LINE.search(a))


# ── 引用局部性：结论与它所引的**那一块**对得上吗 ──────────────────────────
# 为什么需要它（2026-09-21）：no-think 那条路把「AOF 最坏丢多少」答成 **1 秒** ——
# 而资料里 `everysec` 那条才是 1 秒，`no` 那条写的是「持久化没保证」、**根本没给数字**。
# 它把两块的结论混成了一个。
#
# 已有的判据全都看不见这种错：
#   · 数字落地 —— "1秒" 在**整批资料**里找得到（只是不在它引的那块里）✅
#   · 引用有效 —— 编号 1~N 合法 ✅
#   · 逐字重合 —— 逐字像不像，与对不对无关 ✅
#
# 判据是**机械的**：把答案切成带 [n] 的子句，子句里的**具体值**（数字 / 英文标识符）
# 必须出现在**第 n 块**里。它量的不是"有没有出处"，而是**出处的指向对不对**。
_CLAUSE = re.compile(r"[^。；;\n]*?\[\d{1,2}\][^。；;\n]*")
# **只查数字，不查英文术语** —— 实测（2026-09-21）：把英文词也算进来之后，
# 三条报警**全是假阳性**，成因都是**中英术语差异**：
#   资料写「内存区域 / 内存分段」，答案写 `Region`；资料写 `Cset`，答案写 `Collection Set`。
# 数字是硬事实、中英无差异，所以收窄到数字（带单位或 ≥2 位）。
#
# **取值必须停在标点**（`\d+[一-鿿A-Za-z]{0,3}`）：早先允许"数字后跟任意 3 个非空白字符"，
# 于是 `3 层（如 [5] 所示` 抽出来的值是 **`3层（`**（含全角括号），而资料里写的是 `3层(` ——
# 一个括号之差就报假阳性（实测那道 B-tree 题：资料**确实**写了"3层"，被判成对不上）。
# **前面不能是字母**：`G1 每轮` 里的 `1` 是术语 `G1` 的一部分，不是数值 ——
# 不加这条就抽出 `1每轮` 去比对（实测报过两次假阳性）。同理会排掉 `HTTP2`/`SHA256`。
_VALUE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?[ \t]*[一-鿿A-Za-z]{0,3}")


def _values(clause):
    """子句里的**具体值**：带单位的数字（1 秒 / 500ms / 64mb）与英文标识符。

    **单位数必须带单位才算** —— "3" 这种太容易碰巧；而 "1秒" 恰恰是最要紧的那类值
    （第一版把个位数一律滤掉，于是"no 最坏丢 1 秒"这种错**抓不到**）。
    """
    out = []
    # **先把引用编号剥掉**：`第[1][2][5][14]条` 里的 "14" 是编号不是值
    # （实测报过一次假阳性）。
    clause = re.sub(r"\[\d{1,2}\]", " ", clause)
    for v in _VALUE.findall(clause):
        v = re.sub(r"\s+", "", v)
        head = re.match(r"\d+", v)
        if head and len(head.group()) < 2 and len(v) < 2:
            continue                      # 裸的个位数，丢弃
        out.append(v)
    return out


def citation_local(answer, sources, min_len=6):
    """逐子句查「结论里的具体值在不在它所引的那块里」。

    返回 (查过的子句数, 对不上的子句列表)。**对不上 ≠ 一定错** ——
    概括性表述本来就不含具体值；所以只报数，由人看。
    """
    texts = {}
    for i, s in enumerate(sources or [], 1):
        t = s.get("_full")
        if t is None:
            try:
                import os
                import sys
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from ruler import corpus
                C = corpus.load()
                k = next((j for j in range(C.n)
                          if C.doc[j] == s.get("docName") and C.seq[j] == str(s.get("seq"))), None)
                t = C.body[k] if k is not None else (s.get("preview") or "")
            except Exception:
                t = s.get("preview") or ""
        texts[i] = re.sub(r"\s+", "", t)
    checked, bad = 0, []
    for m in _CLAUSE.finditer(answer or ""):
        clause = m.group(0)
        ids = [int(x) for x in re.findall(r"\[(\d{1,2})\]", clause)]
        vals = _values(clause)
        if not ids or not vals:
            continue
        checked += 1
        miss = [v for v in vals if not any(v in texts.get(i, "") for i in ids)]
        if miss:
            bad.append((clause.strip()[:60], ids, miss[:3]))
    return checked, bad


# ── 按题型给判据 ────────────────────────────────────────────────────────
def judge(kind, answer, sources, question):
    """返回 {判据名: True/False/None}，None = 该题不适用这一条。"""
    ctx = (question or "") + "\n" + "\n".join(s.get("preview", "") for s in sources)
    r = {}
    grounded, total, bad = numbers_grounded(answer, ctx)
    r["数字落地"] = f"{grounded}/{total}" if total else "—"
    r["_数字未落地"] = bad[:6]
    r["无标签泄漏"] = not label_leak(answer)
    nc, badc = citation_local(answer, sources)
    if nc:
        r["引用对得上"] = f"{nc - len(badc)}/{nc}"
        r["_引用对不上"] = badc[:3]
        # **引用全对**做成布尔，供 gate 用。它只查**带 [n] 的子句** ⇒
        # 标了「以下为通用知识」的部分天然豁免（那部分本来就不该有编号）。
        r["引用全对"] = (len(badc) == 0)
    vr = verbatim_ratio(answer, sources)
    if vr is not None:
        r["逐字重合"] = f"{100*vr:.0f}%"
        # **不是 pass/fail，是诊断维度** —— 摘抄本身不算"错"（用户可能就要原文），
        # 但它必须**看得见**：分工版 86% / 组织版 21% 是两件完全不同的东西。
        r["_复读"] = vr >= VERBATIM_FLAG
    lc = longest_copy(answer, sources)
    if lc is not None:
        r["最长照抄段"] = f"{lc}字"
        r["没整段照抄"] = lc < COPY_RUN
    r["没抄提示词"] = not echoes_prompt(answer)

    if kind == "grounded":                      # 【乙】有资料
        r["有引用"] = has_cite(answer)
        r["引用有效"] = cites_valid(answer, len(sources))
        # 【乙】的契约是「不得编造资料里没有的内容」。数字**逐字**落地率过低
        # ⇒ 基本可以断定把通用知识掺进了有据的回答（实测那道 AOF 题：来源只说了
        # 「最多丢失 1 秒」，答案却写出「0 秒」「几十秒」——1/6）。
        # 阈值取 0.5：模型自己*推导*出的个别值（如「不丢失」写成「0 秒」）不该误伤，
        # 但 1/6 这种断崖要挂。
        r["数字有据"] = None if not total else (grounded / total >= 0.5)
    elif kind == "grounded-partial":
        # 【乙·细节缺失】库里**有相关内容、但没有那个具体值**。
        #
        # 契约**没覆盖**这种情况（三段式只管"有资料"与"没资料"两端），所以它是块空白：
        # 正确行为应当是「用资料里有的回答 + 明确指出资料未给出该值」，
        # 而**最常见的失败是拿旁边那块的值顶上**（实测那道 no-think 的 AOF 题：
        # 把 everysec 的"1 秒"安给了 no）。
        #
        # 判据用**引用局部性**：它只查**带 [n] 的子句** ⇒ 标了「通用知识」的部分天然豁免
        # （那部分本来就不该有编号）。**不用 `数字有据`** —— 因为它会给"正确地补充了
        # 通用知识并标注"的答案判假失败（那个值本来就不在资料里）。
        r["有引用"] = has_cite(answer)
        r["引用有效"] = cites_valid(answer, len(sources))
        r["声明了没有"] = declares_missing(answer)
        r["标注了通用知识"] = marks_general(answer)
        # **两个合法出口**（这正是它和【乙】的区别：库里有没有那个值，事先不知道）：
        #   ① 资料里有 → 引用它（有引用 + 有效 + 全对）
        #   ② 资料里没有 → 如实声明 + 标注通用知识
        # 第一版只认①，于是把三条**答得很诚实**的判成了失败
        # （"知识库中没有……的具体资料"被记为 有引用=False）。判据自己错了。
        r["走对了出口"] = (
            (has_cite(answer) and cites_valid(answer, len(sources)) is not False and not badc)
            or (declares_missing(answer) and marks_general(answer))
        )
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
    # **`没抄提示词` 全类都要** —— 把提示词回声出来与题型无关，它不是"答得不好"，
    # 是"根本没答"。（实测抓到的那条，答案最后还把**问题本身**列成了"通用知识"。）
    #
    # `没整段照抄` 只加在**有资料的**两类上：没有资料时无从照抄（判据会返回 None）。
    "grounded": ["有引用", "引用有效", "数字有据", "无标签泄漏",
                 "没抄提示词", "没整段照抄"],
    # 新增的**难题类**：库里**有相关内容、但没有那个具体值**。
    # 契约没覆盖这种情况，而最常见的失败是"拿旁边那块的值顶上" ——
    # `引用全对`正好拦它（值不在它引的那块里）。
    #
    # **不加 `数字有据`**：那会给"正确地补充了通用知识并标注"的答案判假失败
    # （那个值本来就不在资料里）—— 而标注过就是合法行为。
    "grounded-partial": ["走对了出口", "无标签泄漏", "没抄提示词", "没整段照抄"],
    "ungrounded": ["声明了没有", "标注了通用知识", "无标签泄漏", "没抄提示词"],
    "chitchat": ["没误报「知识库没有」", "无标签泄漏", "没抄提示词"],
}
