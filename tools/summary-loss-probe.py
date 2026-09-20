# -*- coding: utf-8 -*-
"""
摘要合并的「只增不删」：把着落判据从**挑选**升级成**兜底**。

现状（SummaryService）：合并三次，按「旧条目没着落的条数 → 总条数」挑最好的一版。
着落判据只用来**挑**，三次都漏掉的那条就真的没了 —— 这正是「零星丢失」那个已知缺口。

合成思路：判据已经在算了（`unrepresented`），**把它从"评分"改成"补丁"** ——
合并结果里没着落的旧条目，原样补回列表尾部。零额外调用。

两条臂，各跑 REPS 条链，每条链 5 轮增量合并：
  A 现状（best-of-3 挑选）
  B 现状 + 把没着落的旧条目原样补回

判据（每轮都记）：
  · 8 个种子事实的存活数（任一可接受字面量还在就算活）
  · 条目数（会涨吗）
  · 重复对（归一化后共享 ≥6 字连续子串的条目对——补回会不会造重）

用法：python tools/summary-loss-probe.py [链数，默认 2]
"""
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
# 用法：python tools/summary-loss-probe.py [链数] [臂] [模型] [num_gpu]
# 例：python tools/summary-loss-probe.py 2 A qwen3:8b 26
CHAT = sys.argv[3] if len(sys.argv) > 3 else "qwen3:4b"
NUM_GPU = int(sys.argv[4]) if len(sys.argv) > 4 else None
ATTEMPTS = 3

spec = importlib.util.spec_from_file_location("ssp", os.path.join(HERE, "summary-shape-probe.py"))
ssp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssp)
PROMPT, SCHEMA = ssp.DELTA_PROMPT, ssp.DELTA_SCHEMA

# 种子事实：字面量任一中出现即算存活（被改写的接受新旧两值）
FACTS = [
    ("分块粒度", ["600", "450"]),
    ("用户偏好", ["表格"]),
    ("向量模型", ["bge-m3"]),
    ("窗口上限", ["10240", "16384", "24576", "32768"]),
    ("否决方向", ["DP 选块"]),
    ("摘要窗口", ["16 条", "16条", "20 条"]),
    ("裁剪顺序", ["共进退"]),
    ("嵌入文本", ["语境行"]),
]

SEED = """分块粒度：— → 600 字
用户偏好：— → 表格而非选项式
向量模型：— → bge-m3
窗口上限：— → 10240
否决方向：DP 选块（候选装得下，收益为 0）
摘要窗口：— → 16 条
裁剪顺序：— → 摘要与召回片段共进退
嵌入文本：— → 块文本前面拼一行语境行"""

# **把初始列表垫到接近上限**（KB_SEED_PAD=n）：要问的只是"撞到 MAX_ITEMS=20 之后
# 只增不删还保不保护"，这个条件可以直接造，不必手写十几轮对话。
# 垫的是**本项目的真事实**（都是"没变化"那类 —— 正是模型最爱丢的），
# 判据仍只盯原来那 8 个种子，所以垫了不影响读数。
PAD = [
    "云端仓库：— → 不暴露网址",
    "分块方式：— → 按句子边界",
    "PDF 解析：— → PDFBox",
    "向量维度：— → 1024",
    "检索融合：— → RRF",
    "摘要写法：— → 一行一条",
    "会话摘要：— → 变化式",
    "传输加密：— → RSA+AES 混合",
    "认证：— → 双令牌",
    "KV 量化：— → q4_0",
    "抓取：— → 输出 Markdown",
    "主题树：— → 概览进提示词",
]
_pad_n = int(os.environ.get("KB_SEED_PAD", "0"))
if _pad_n:
    SEED = SEED + "\n" + "\n".join(PAD[:_pad_n])
    print(f"（初始列表垫到 {len([x for x in SEED.split(chr(10)) if x.strip()])} 条，"
          f"MAX_ITEMS=20）", flush=True)

ROUNDS = [
    "用户：块大小 600 有点大，抽不出事实，改成 450 试试。\n"
    "助手：450 字边界会更碎，召回条数会变多，先在小范围试。\n"
    "用户：另外以后每次改完代码，都要给我一份改动文件清单。\n"
    "助手：好，清单里带一句话摘要。",

    "用户：云端仓库那个 README 别把部署网址写进去。\n"
    "助手：已改成占位符，网址只留本地。\n"
    "用户：窗口上限既然实测能到 24576，配置就按 24576 写。\n"
    "助手：好，和 prompt-window-tokens 保持一致。",

    "用户：摘要窗口 16 条太短了，改成 20 条。\n"
    "助手：20 条会让裁剪顺序那边更早丢摘要。\n"
    "用户：那就先不动，记着这个代价。\n"
    "助手：记下了：摘要窗口变长会挤压召回片段。",

    "用户：数字被糊成「60:」那个问题，加个机械判据吧。\n"
    "助手：只记日志不改内容，修复规则风险太大。\n"
    "用户：对，先能发现就行。\n"
    "助手：已在摘要服务里加了判据。",

    "用户：再确认一下，嵌入用的文本前面那行语境行，是不进提示词的对吧。\n"
    "助手：对，只进索引，不进提示词，查询侧零开销。\n"
    "用户：好，这条别忘了。\n"
    "助手：已记住。",
]

# 每轮必须**真的进去**的新信息（字面量任一中出现即算落地）。
# 没有这一列，度量会瞎：4b 开思考时"存活 8/8、条目一条不涨"，看着完美，
# 实际是**把旧列表原样抄回来**（冻结记忆）—— 一种与"丢事实"方向相反的退化解。
UPDATES = [
    ["450"],                     # 分块粒度 600 → 450
    ["24576", "占位符"],          # 窗口上限改 24576；README 网址改占位符
    ["20"],                      # 摘要窗口 16 → 20
    ["机械判据", "判据"],          # 数字加机械判据
    ["不进提示词", "只进索引"],     # 语境行不进提示词
]


def post(system, user, timeout=180, nonce=None):
    if nonce:
        # 破前缀：Ollama 会复用逐字相同前缀的 KV，而"重试"发的正是同一条提示词 ——
        # 于是三次重试很可能拿到同一个塌陷结果（batch-collapse-probe 已证 A/C 组差异）
        system = system + f"\n\n（编号 {nonce}）"
    opts = {"temperature": 0.2, "num_ctx": 8192}
    if NUM_GPU is not None:
        opts["num_gpu"] = NUM_GPU      # 8b 必须钉住层数，否则会把 bge-m3 挤出显存
    if os.environ.get("KB_NUM_PREDICT"):
        opts["num_predict"] = int(os.environ["KB_NUM_PREDICT"])
    think = os.environ.get("KB_THINK") == "1"
    body = {"model": CHAT, "stream": False, "think": think, "format": SCHEMA,
            "options": opts,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("message", {}).get("content", "")
        except Exception:
            time.sleep(2)
    return ""


def items_of(raw):
    try:
        return [str(x).strip() for x in (json.loads(raw).get("items") or []) if str(x).strip()]
    except Exception:
        return []


def norm(s):
    return re.sub(r"\s+", "", s.replace("→", " ").replace("—", " "))


def counterpart(a, b):
    """与 Java 的 hasCounterpart 同规则：归一化后最长公共子串 ≥ min(6, len/2)"""
    x, y = norm(a), norm(b)
    need = min(6, max(2, len(x) // 2))
    prev = [0] * (len(y) + 1)
    for i in range(1, len(x) + 1):
        cur = [0] * (len(y) + 1)
        for j in range(1, len(y) + 1):
            if x[i - 1] == y[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] >= need:
                    return True
        prev = cur
    return False


def unrepresented(old_items, items):
    return [o for o in old_items
            if not any(counterpart(o, it) for it in items)]


def merge(old, fresh, arm):
    """一次合并。

    arm 两位：破前缀 / 只增不删
      A 现状（best-of-3，三次同题）
      B 现状 + 没着落的旧条目原样补回
      C 三次重试各带唯一编号（破前缀复用）
      D C + B
    """
    break_prefix, append_missing = {"A": (False, False), "B": (False, True),
                                    "C": (True, False), "D": (True, True)}[arm]
    old_items = [x.strip() for x in old.split("\n") if x.strip()]
    user = f"已有条目：\n{old if old else '（无，这是第一次）'}\n\n新增对话：\n{fresh}"
    best, missing = [], old_items
    for k in range(ATTEMPTS):
        nonce = f"{int(time.time()*1000)%100000}-{k}" if break_prefix else None
        items = items_of(post(PROMPT, user, nonce=nonce))
        cand_missing = unrepresented(old_items, items)
        if not best or (len(cand_missing), -len(items)) < (len(missing), -len(best)):
            best, missing = items, cand_missing
        if best and len(best) >= len(old_items) and not missing:
            break
    if not best:
        return old, [], len(missing)
    if os.environ.get("KB_DEBUG"):
        print(f"    [调试] old_items={len(old_items)} best={len(best)} missing={len(missing)} "
              f"best前2={best[:2]}", flush=True)
    appended = []
    if append_missing and missing:
        for o in missing:
            if not any(counterpart(o, it) for it in best) and len(best) + len(appended) < 20:
                appended.append(o)
    # 多返一个 **missing 条数**：把「上限挡住了补回」与「本来就没丢」分开 ——
    # 只看 appended 的话这两种情况长得一模一样（都是 0）。
    return "\n".join(best + appended), appended, len(missing)


def alive(items, joined):
    return sum(1 for _, lits in FACTS if any(l in joined for l in lits))


def dup_pairs(items):
    n = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if counterpart(items[i], items[j]):
                n += 1
    return n


def split_change(it):
    """切成「曾经」「现在」两半；没有箭头时整条算「现在」。"""
    if "→" in it:
        a, b = it.split("→", 1)
        return a.strip(), b.strip()
    return "", it.strip()


def stale_count(items):
    """有几条的「现在」已经被**别的条目**写进了「曾经」—— 即留着的陈旧行。"""
    rights = []
    lefts = []
    for it in items:
        a, b = split_change(it)
        rights.append(norm(b))
        lefts.append(norm(a))
    n = 0
    for i, r in enumerate(rights):
        r = r.strip("—")
        if len(r) < 4 or r in ("无", "保持不变"):
            continue
        for j, l in enumerate(lefts):
            if i != j and r and r in l:
                n += 1
                break
    return n


def chain(arm):
    old, log = SEED, []
    for r, fresh in enumerate(ROUNDS, 1):
        old, appended, nmiss = merge(old, fresh, arm)
        items = [x for x in old.split("\n") if x.strip()]
        joined = " ".join(items)
        landed = sum(1 for lits in UPDATES[r - 1] if any(l in joined for l in lits))
        log.append((r, alive(items, joined), len(items), dup_pairs(items),
                    len(appended), stale_count(items), landed, nmiss))
    return old, log


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    arms = sys.argv[2].upper() if len(sys.argv) > 2 else "ABCD"
    label = {"A": "现状（best-of-3）", "B": "只增不删（补回没着落的）",
             "C": "破前缀（重试各带唯一编号）", "D": "破前缀 + 只增不删"}
    summary = {}
    for arm in arms:
        print(f"===== 臂 {arm}（{label[arm]}） =====")
        for rep in range(1, reps + 1):
            t0 = time.time()
            final, log = chain(arm)
            print(f"  链{rep}（{time.time()-t0:.0f}s）")
            for r, a, n, d, ap, st, ld, nm in log:
                flag = "  ← 没位置了" if (nm > ap and ap == 0) else ""
                print(f"    轮{r}  种子存活 {a}/8   新信息落地 {ld}/{len(UPDATES[r-1])}   "
                      f"条目 {n:>2}   重复对 {d}   陈旧行 {st}   缺{nm}  补回 {ap}{flag}")
            if rep == 1:
                print("    最终条目：")
                for x in final.split("\n"):
                    print(f"      · {x}")
            summary.setdefault(arm, []).append(log)
        print(flush=True)

    print("—— 汇总：每轮种子存活均值（8 个种子）/ 末轮条目数均值 ——")
    print(f"{'轮':>4}" + "".join(f"{'臂'+a:>12}" for a in arms))
    for r in range(len(ROUNDS)):
        row = f"{r+1:>4}"
        for a in arms:
            row += f"{sum(x[r][1] for x in summary[a]) / len(summary[a]):>12.1f}"
        print(row)
    print(f"\n末轮条目数：" + "  ".join(
        f"臂{a} {sum(x[-1][2] for x in summary[a]) / len(summary[a]):.1f}" for a in arms))
    print("末轮陈旧行：" + "  ".join(
        f"臂{a} {sum(x[-1][5] for x in summary[a]) / len(summary[a]):.1f}" for a in arms))
    print("末轮新信息落地：" + "  ".join(
        f"臂{a} {sum(x[-1][6] for x in summary[a]) / len(summary[a]):.1f}" for a in arms))
    print("（补回的代价是列表变长、可能留下陈旧行；"
          "**落地那一列不能省** —— 它区分「真合并」与「把旧列表原样抄回来」）")


if __name__ == "__main__":
    main()
