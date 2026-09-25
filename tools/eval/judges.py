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
#
# ⚠️ **2026-09-25 扩过一次**，起因：一道题的答案写着「没有明确指出…总数」「未能给出…完整清单」，
# 判据却报「声明了没有=False」—— **字段在说谎**（我据此差点把模型判成没声明）。
# 原来那五条是照着**几个样本**写的，从没在真实答案上复核过。
#
# 扩之前先普查了 **1327 条落盘答案**（`data/_probe-declare*.py`，跑完即删）：
#   · 真实措辞的主族是 **「资料/知识库 + 未（明确）说明/给出/提供/提及」** —— 整整一族漏掉
#     （老判据只认「未找到 / 未收录」）
#   · 候选逐条量：出处+未/没有 **40** 条、没有+动词 **4** 条、未找到/未见 **2** 条；
#     **落在 chitchat/capability（"误报知识库没有"那条判据管的）里的：0 条**
#   · 一条不带主语的 `未(明确)?(说明|…)` 曾入选，实测**独立贡献 0 条**（全被上面覆盖）⇒ 去掉 ——
#     它正是最可能误伤【甲】类的形状，没有收益就不留
#   · **改完把 1327 条历史结论重判一遍：翻转 0 条**
# ⇒ 这次改动**不改变任何结论**，它改的是**诊断行是否可信**（`声明了没有` 是判死时要读的证据）。
#   契约本身要的是**意思**（"① 第一句先说明知识库中没有这方面的资料"，例子只是例如），
#   所以接受同义说法是对的。
_MISSING = re.compile(
    r"知识库(中|里)?(并)?(没有|不含|未收录)|资料(中|里)?没有|没有(相关|这方[面向]的)?(资料|信息)"
    r"|知识库(中|里)?未(找到|收录)|未找到相关"
    r"|(资料|知识库|参考资料|文献)(中|里)?(都|均)?(未|没有|不含)"
    r"|没有(明确)?(指出|说明|给出|提供|提及|收录|记录)"
    r"|(未|没有)(找到|发现|见到|查[到询]|检索到)")
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
# ⚠️ **不限行首**（2026-09-25 修）：第一版只认行首的标签，于是漏掉了嵌在句子里的那种 ——
# 实测答案里写着「知识库中没有关于…的详细资料（**系统判定为【丙】**）」，
# 而判据看不见它。分类标签是**契约的内部词汇**，出现在正文的任何位置都是泄漏。
# （`【参考资料】/【问题】/【知识库主题概览】` 不在此列 —— 那是契约允许正文提及的词，
#   它们由 `_SCAFFOLD_LINE` 单独管，只认"独占一行"的形态。）
_LABEL = re.compile(r"【[甲乙丙]】|^\s*【(知识性|与知识库)")


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


def _source_texts(sources):
    """召回来源 → {编号: **模型真看到的那些字**}（去掉空白）。

    **优先级：`injected` > 块正文**（2026-09-25 加）。分层注入只把块里挑中的几句给模型，
    拿整块去比会**多报**"该引没引"——实测 14 题里多报 4/11（那 4 个值模型从没见过）。
    仪器必须比的是"它看到了什么"，不是"库里有什么"。
    （`injected` 由出口带上来，见 `ChunkHit#injected`；老落盘结果没有这一项，会退回块正文。）
    """
    texts = {}
    for i, s in enumerate(sources or [], 1):
        t = s.get("injected") or s.get("_full")
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
    return texts


def citation_local(answer, sources, min_len=6):
    """逐子句查「结论里的具体值在不在它所引的那块里」。

    返回 (查过的子句数, 对不上的子句列表)。**对不上 ≠ 一定错** ——
    概括性表述本来就不含具体值；所以只报数，由人看。
    """
    texts = _source_texts(sources)
    checked, bad = 0, []
    for m in _CLAUSE.finditer(_attach_cites(answer)):
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


# ── 引用局部性的**另一半**：该引没引 ────────────────────────────────────
# 上面那半只看**带 [n] 的子句** ⇒ 一句一个引用都没有的话**从缝里漏过去**。
# 症状（2026-09-25 实测）：一道题的答案列了四个**来自资料**的数字
# （15金 / 6金 / 5金 / 1金），一个 `[n]` 都没标 —— 而判据只能报一句 `有引用=False`，
# **说不出"哪几句该引没引"**，于是看着像"没检索到"，其实是**引用纪律**。
#
# 契约对这件事有明确的两句：「引用【参考资料】的地方用 [编号] 标注；
# 通用知识部分不要标 [编号]，否则会让人误以为有出处」。所以判据是：
#   **值在召回资料里 + 这句没有任何引用 = 该引没引**（值不在资料里 = 通用知识，本来就不该标）
#
# ⚠️ **只报数、不当判据**（第一版刻意如此）：数字撞车是常见现象（年份、序号、
# 短数字），把它做成硬判据会凭空造失败。先量它在真实答案上的分布，再谈要不要升级。
_CLAUSE_ANY = re.compile(r"[^。；;\n]+")

# **引用标在句号之后**：`…9月25日。[2][8]` ⇒ `…9月25日[2][8]。`
# 为什么（2026-09-25 实测）：中文答案常把出处标在**句末句号之后**，而子句是按标点切的，
# 于是那半句变成"有值、没引用"的样子 —— **两个判据各错一边**：
#   · `uncited_values` **多报**"该引没引"（其实标了；实测 nl-g5 就是这么来的）
#   · `citation_local` **漏检**（它只查句内带引用的子句 ⇒ 这半句从来没被查过）
# 实测 1355 条答案里 **62 条（5%）** 有这个形态。挪一下位置，两边同时归位。
_TRAIL_CITE = re.compile(r"([。；;])([ \t]*)((?:\[\d{1,2}\])+)")


def _attach_cites(answer):
    """把跟在标点**之后**的引用挪到标点**之前**（见 `_TRAIL_CITE`）。"""
    return _TRAIL_CITE.sub(lambda m: m.group(3) + m.group(1), answer or "")

# **这一半必须比 `_VALUE` 更严** —— 两轮实测定下来的（2026-09-25，1327 条真实答案）：
#   第一版直接复用 `_values`（它允许"≥2 位的裸数字"）⇒ 324 条命中，误伤成灾：
#     · 裸数字撞车：`10` / `100` / `1000` / `20` / `85` —— 随便一块里都有，纯巧合
#     · **复合字面量被切碎**：时间戳 `'2023-10-01 12:00:00'` ⇒ `['2023','10','01','12']`；
#       引用计数 `参考资料（1-13）` ⇒ `['13']`
#   第二版收紧成"只认带单位的数字" ⇒ 156 条；剩下的误伤就一种：**量词当单位**（`1个`）。
#   所以第三版把"哪些算单位"**写成白名单**（不再用 `[一-鿿]{1,3}` 放行一切汉字）：
#   量词（个/条/项/次/种/位/名/份）信息量太低，任何文本里都有 —— 排除。
# 代价是漏掉一些真裸数字/真量词值 —— 这一条是**读数**不是判据，**宁可少报也不要噪音**。
_UNIT_STRICT = re.compile(
    r"(?<![A-Za-z0-9.:/\-])"                       # 前面不能是字母/数字/小数点/冒号/斜杠/连字符
    r"(\d+(?:\.\d+)?[ \t]*"
    r"(?:秒|分钟|小时|天|年|月|日|周|倍|层|亿|万|金|人|岁|字|页|位次"
    r"|%|ms|s|sec|min|h|KB|MB|GB|TB|kb|mb|gb|bit|byte|n|p))"
    r"(?![0-9])")                                  # 后面不能直接跟数字（`10-30` / `1.5.2`）


def _values_strict(clause):
    """**带单位的**具体值 —— 专供"该引没引"这条用（见 `_UNIT_STRICT` 的取舍说明）。"""
    clause = re.sub(r"\[\d{1,2}\]", " ", clause)
    return [re.sub(r"\s+", "", v) for v in _UNIT_STRICT.findall(clause)]


def uncited_values(answer, sources):
    """没标引用、但**召回资料里确实有**的**带单位**具体值。

    返回 [(子句, [值...], [它出现在哪几块...])]。空列表 = 该引的都引了。
    """
    texts = _source_texts(sources)
    out = []
    for m in _CLAUSE_ANY.finditer(_attach_cites(answer)):
        clause = m.group(0)
        if re.search(r"\[\d{1,2}\]", clause):
            continue                       # 带引用的交给 citation_local
        vals = _values_strict(clause)
        if not vals:
            continue
        found = {}
        for v in vals:
            where = [i for i, t in texts.items() if v in t]
            if where:
                found[v] = where
        if found:
            out.append((clause.strip()[:70], list(found), sorted({i for w in found.values() for i in w})))
    return out


# ── 按题型给判据 ────────────────────────────────────────────────────────
_SENT_CACHE = None


def sentence_citations(cites, sources):
    """**句子级引用指得到句子吗** —— 机械可判，且抓得到编造。

    2026-09-23 连着两轮实测到同一件事：**注入的单位比"块"更细时，模型自发按句子粒度引用**
    （给了地址那轮写 `[1.7]`；**没给地址**的分层注入那轮自己编 `[4.3]`、`[2.11]`）。
    后端已经把它们规范化成 `[n]`（见 `CiteFix`），句号那半截随 `stats.cites` 出来 ——
    这一条就是量它指得准不准。

    ⚠️ **只判"指得到"，不判"指得对"**：规范化把 `[4.3]` 变成了 `[4]`，
    "这一句支不支持那句结论"所需的**位置信息有意丢掉了**（换来判据与前端不用改）。
    要判"对不对"得再留一份位置映射 —— **留到真需要时再做**，不在这里假装能判。

    返回 (有效数, 总数)。指到不存在的段/句就是编造。
    """
    global _SENT_CACHE
    if not cites:
        return (0, 0)
    try:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ruler import corpus
        C = corpus.load()
        if _SENT_CACHE is None:
            rows = corpus.psql_rows("SELECT chunk_id, seq FROM sentences")
            _SENT_CACHE = {}
            for cid, sq in rows:
                _SENT_CACHE.setdefault(cid, set()).add(int(sq))
    except Exception:
        return (0, 0)
    ok = tot = 0
    for one in str(cites).split(","):
        one = one.strip()
        if not one or "." not in one:
            continue
        n_s, m_s = one.split(".", 1)
        m_s = m_s.split("@", 1)[0]              # 偏移（@ 后面那半）在这条判据里不用
        try:
            n, m = int(n_s), int(m_s)
        except ValueError:
            continue
        tot += 1
        if not (1 <= n <= len(sources or [])):
            continue
        s = sources[n - 1]
        i = next((k for k in range(C.n)
                  if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
        if i is not None and m in _SENT_CACHE.get(C.ids[i], ()):
            ok += 1
    return (ok, tot)


def sentence_citation_local(cites, sources, answer):
    """**句子级引用指得对吗**：把每个 `[n.m]` 所在子句里的**具体值**，
    拿去比它指的那**一句**（而不是整块）。

    这是 `citation_local`（块级）往下沉一层。判据形状照搬它，理由也一样：
    **只查"具体值在不在"，不查"整体支不支持"** —— 后者要靠语义，
    而语义判据在本项目里反复指错方向（`2026-09-21-少想这条线收口` 那五条）。
    概括性表述本来就不含具体值 ⇒ **对不上 ≠ 一定错**，所以只报数、由人看。

    有了 `@偏移`，才知道这个引用**挂在哪句结论上** —— 这是它与"只判指得到"的区别。

    ⚠️ **已知的假阳性形状（实测抓到第一个就长这样，记下来免得下一个人重新踩）**：
    一条结论挂**两个引用**时，两个都被独立检查 —— 而值往往只在前一个里。
    实例：`-XX:G1MixedGCLiveThresholdbeiPercent（默认85%）…[2][11]`，
    `85` 在 `[2.6]`（「…默认为85%…」）里、不在 `[11.2]`（「…低于此值…」）里
    ⇒ 判 `[11.2]` 对不上，而那一句其实**正确地补充了另一件事**。
    与块级的 `引用对得上` 是同一族的形状（那条的契约就是**只报数，由人看**），
    所以这里也**只报数、不进 PASS** —— 它衡量的是"句级引用的精度"，不是"这题答得好不好"。

    返回 (查过的条数, 对不上的列表)。
    """
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ruler import corpus
    C = corpus.load()
    texts = {}
    checked, bad = 0, []
    for one in str(cites or "").split(","):
        one = one.strip()
        if "@" not in one or "." not in one:
            continue
        nm, off = one.split("@", 1)
        n_s, m_s = nm.split(".", 1)
        try:
            n, m, off = int(n_s), int(m_s), int(off)
        except ValueError:
            continue
        if not (1 <= n <= len(sources or [])):
            continue
        key = (n, m)
        if key not in texts:
            s = sources[n - 1]
            i = next((k for k in range(C.n)
                      if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
            t = ""
            if i is not None:
                rows = corpus.psql_rows(
                    "SELECT text FROM sentences WHERE chunk_id = %s AND seq = %s"
                    % (C.ids[i], m))
                t = rows[0][0] if rows else ""
            texts[key] = re.sub(r"\s+", "", t)
        if not texts[key]:
            continue                     # 句子查不到 —— 那是「句级引用有效」那一条的事
        # 引用**挂在哪句结论上**：从最近的一个句末/换行切到引用处
        clause = re.split(r"[。；\n]", (answer or "")[:off])[-1]
        vals = _values(clause)
        if not vals:
            continue                     # 概括性表述不含具体值 ⇒ 这一条不适用
        checked += 1
        miss = [v for v in vals if v not in texts[key]]
        if miss:
            bad.append((clause.strip()[:50], f"[{n}.{m}]", miss[:3]))
    return checked, bad


def judge(kind, answer, sources, question, cites=None):
    """返回 {判据名: True/False/None}，None = 该题不适用这一条。

    {@code cites} = 后端规范化时剥出来的句子级引用（`"4.3,2.11"`），
    来自 `stats` 事件。**没有它就没法量句级引用** —— 因为答案文本里只剩 `[4]` 了。
    """
    # **用完整块正文，不能用 preview** —— preview 只有 ~200 字。
    # 2026-09-22 实测踩到：答案写了 `-XX:G1MixedGCLiveThresholdPercent（默认85%）`，
    # 而 85 在**完整块里**（519 字那块）、只是落在了 200 字 preview 之外
    # ⇒ 判成「未落地」。**同一个文件里 `_full_sources()` 是现成的**，
    # `verbatim_ratio` / `longest_copy` 都在用它，只有这一条在用 preview。
    # 与台账里「概览被截断当成全集」（grounded 50%→83%）是**同一个坑的第二次**：
    # **截断的输入会让判据把"没看见"当成"不存在"。**
    ctx = (question or "") + "\n" + _full_sources(sources)
    r = {}
    grounded, total, bad = numbers_grounded(answer, ctx)
    r["数字落地"] = f"{grounded}/{total}" if total else "—"
    r["_数字未落地"] = bad[:6]
    r["无标签泄漏"] = not label_leak(answer)
    # **句级引用**：只报数不进 PASS —— 它衡量的是"细粒度注入下模型引用的精度"，
    # 不是"这题答得好不好"。做成通过条件反而会把"用了句级引用"变成一种失败。
    sok, stot = sentence_citations(cites, sources)
    if stot:
        r["句级引用有效"] = f"{sok}/{stot}"
    sc, sbad = sentence_citation_local(cites, sources, answer)
    if sc:
        r["句级引用对得上"] = f"{sc - len(sbad)}/{sc}"
        r["_句级引用对不上"] = sbad[:3]
    nc, badc = citation_local(answer, sources)
    if nc:
        r["引用对得上"] = f"{nc - len(badc)}/{nc}"
        r["_引用对不上"] = badc[:3]
        # **引用全对**做成布尔，供 gate 用。它只查**带 [n] 的子句** ⇒
        # 标了「以下为通用知识」的部分天然豁免（那部分本来就不该有编号）。
        r["引用全对"] = (len(badc) == 0)
    # **该引没引**（引用局部性的另一半）：值在资料里、而这句一个 [n] 都没有。
    # 为什么要有它（2026-09-25）：上面那半只看带引用的子句 ⇒ 一句引用都没有的话
    # **整句从缝里漏过去**，症状是判据只说「有引用=False」，**说不出哪几句该引没引** ——
    # 看着像"没检索到"，其实是**引用纪律**（实测那道亚运金牌题：四个来自资料的数字，
    # `数字落地=1/1` 全过、一个 [n] 都没标）。
    # ⚠️ **只报数，不进 PASS**：数字撞车是常见现象，做成硬判据会凭空造失败
    #（第一版 324 条里大半是误伤，收紧到"带单位的白名单值"后 chitchat/capability 归零）。
    uv = uncited_values(answer, sources)
    if uv:
        r["未标引用的资料值"] = sum(len(v) for _, v, _ in uv)
        r["_未标引用"] = [f"{v}←块{w}" for _, v, w in uv[:3]]
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
    elif kind == "capability":                  # 【甲·自身能力】问助手/知识库自己
        # **为什么单列一类**（2026-09-24 实测）：问「你能帮我做什么」时，
        # 模型去检索"AI 助手功能"、检索不到，于是正确地说了"知识库中没有" ——
        # **它没错，是契约没给这一类出口**。而它的判据与 chitchat 同形
        # （唯一的失败模式就是误报"没有"），所以判据可以复用，只是**题型要分开记**：
        # 混进 chitchat 会让"闲聊答得好不好"与"能力类答得对不对"挤进同一个数字。
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
    # 【甲·自身能力】：唯一的失败模式与 chitchat 同形（误报"知识库没有"），
    # 但分开记 —— 见上面 capability 分支的注释。
    "capability": ["没误报「知识库没有」", "无标签泄漏", "没抄提示词"],
}
