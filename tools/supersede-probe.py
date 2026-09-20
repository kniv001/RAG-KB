# -*- coding: utf-8 -*-
"""
**覆盖边**：旧值被推翻时，记忆输出的是「A → B」的关系，还是 A、B 两条并存？

为什么这台机器要单独造（台账「待验证」第 1 条）：
  现状只是**碰巧**压成一行，**未经设计验证**；而且旧值仍留在里面。
  而这是整条记忆线的根本问题 ——
  「库里留着被撤回的结论，问到时会被当事实端出」（D 组已证）。

判据（每个被推翻的主题，输出必须落进且只落进其中一档）：

  ✅ **覆盖**：该主题**恰好一条**，且写成「旧 → 新」（旧值明确标在左边）
  ⚠ **并存**：该主题出现**两条及以上**（旧一条、新一条）—— 读者无法判断哪个是当前值
  ❌ **丢新**：只剩旧值，新值根本没进去
  ❌ **丢旧**：只剩新值，看不出"变过"（提示词第 5 条明确要求别这样）

另外盯一个**没被推翻**的主题（对照组）：它照样要写出来（提示词第 4 条）。

用法：python tools/supersede-probe.py [次数，默认 12] [模型]
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

spec = importlib.util.spec_from_file_location("ssp", os.path.join(HERE, "summary-shape-probe.py"))
ssp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssp)
PROMPT, SCHEMA = ssp.DELTA_PROMPT, ssp.DELTA_SCHEMA

OLD = """分块粒度：— → 600 字
摘要窗口：— → 16 条
向量模型：— → bge-m3
用户偏好：— → 表格而非选项式
裁剪顺序：— → 摘要与召回片段共进退
KV 量化：— → q4_0
嵌入文本：— → 块文本前面拼一行语境行
认证：— → 双令牌"""

# 推翻两个、稳住一个（对照组）
FRESH = ("用户：块大小 600 有点大，抽不出事实，改成 450 试试。\n"
         "助手：450 字边界会更碎，召回条数会变多，先在小范围试。\n"
         "用户：摘要窗口 16 条也太短了，改成 20 条。\n"
         "助手：20 条会让裁剪顺序那边更早丢摘要。\n"
         "用户：那就先这样。另外向量模型还是用 bge-m3 不变。\n"
         "助手：好，不变。")

# 主题 → (旧值, 新值)；第三个是**没变**的对照
SUPERSEDED = [("分块粒度", "600", "450"), ("摘要窗口", "16", "20")]
STABLE = [("向量模型", "bge-m3")]



def parse_item(it):
    """「主题：曾经 → 现在」→ (主题, 曾经, 现在)；不是这个形状就 None。"""
    if "：" not in it or "→" not in it:
        return None
    key, rest = it.split("：", 1)
    l, r = rest.split("→", 1)
    return key.strip(), l.strip(), r.strip()


def add_edges(old_lines, new_items):
    """**机械补覆盖边**：新条目左边是「—」、而这个主题上一条有值时，把上一条的值搬到左边。

    为什么放在代码里而不是提示词里：2026-09-20 实测 —— 提示词里明写「本次被改变时，
    把上一个值写到左边」，模型**照旧 12/12 把旧值丢掉**。而这件事是**纯字符串操作**：
    上一条的右值就是这一条的左值。跟 `missingOld`（只增不删）是同一类补丁。
    """
    prev = {}
    for it in old_lines:
        p = parse_item(it)
        if p:
            prev[p[0]] = p[2]
    out, n = [], 0
    for it in new_items:
        p = parse_item(it)
        if p and p[1] in ("—", "-", "", "无", "？", "?") and prev.get(p[0]) not in (None, "", p[2]):
            it = f"{p[0]}：{prev[p[0]]} → {p[2]}"
            n += 1
        out.append(it)
    return out, n


def post(user, model, timeout=180):
    body = {"model": model, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.2, "num_ctx": 8192},
            "messages": [{"role": "system", "content": PROMPT},
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


def norm(s):
    return re.sub(r"\s+", "", s.replace("—", "").replace("→", "→"))


def classify(items, key, old_v, new_v):
    """这个主题在输出里落进哪一档。"""
    hits = [it for it in items if norm(key) in norm(it)]
    if not hits:
        return "❌丢新" if False else "❌整条没了"
    if len(hits) > 1:
        return "⚠并存"
    it = hits[0]
    has_old = old_v in it
    has_new = new_v in it
    if has_new and has_old and "→" in it:
        return "✅覆盖"
    if has_new and not has_old:
        return "❌丢旧"
    if has_old and not has_new:
        return "❌丢新"
    return "⚠形态怪"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    model = sys.argv[2] if len(sys.argv) > 2 else "qwen3:4b"
    user = f"已有条目：\n{OLD}\n\n新增对话：\n{FRESH}"
    print(f"模型 {model}　跑 {n} 次　推翻 2 个主题 + 稳住 1 个对照\n")

    tally = {k: {} for k, _, _ in SUPERSEDED}
    stable_ok = 0
    weird = []
    for i in range(1, n + 1):
        raw = post(user, model)
        try:
            items = [str(x).strip() for x in (json.loads(raw).get("items") or []) if str(x).strip()]
        except Exception:
            items = []
        nedge = 0
        if os.environ.get("KB_EDGE") == "1":
            items, nedge = add_edges([x for x in OLD.split("\n") if x.strip()], items)
        row = []
        for k, o, nv in SUPERSEDED:
            v = classify(items, k, o, nv)
            tally[k][v] = tally[k].get(v, 0) + 1
            row.append(f"{k}={v}")
        st = [it for it in items if any(norm(x) in norm(it) for x, _ in STABLE)]
        ok = len(st) == 1 and all(v in st[0] for _, v in STABLE)
        stable_ok += ok
        row.append(f"对照({'✅' if ok else '❌'})")
        if os.environ.get("KB_EDGE") == "1":
            row.append(f"补边{nedge}")
        print(f"  {i:>2}. " + "  ".join(row), flush=True)
        if any("怪" in x or "并存" in x for x in row) and len(weird) < 3:
            weird.append([it for it in items if "分块" in it or "摘要窗口" in it])

    print()
    for k, _, _ in SUPERSEDED:
        total = sum(tally[k].values())
        parts = "　".join(f"{v} {c}/{total}" for v, c in sorted(tally[k].items()))
        print(f"  {k:<8}{parts}")
    print(f"  对照组（没变的主题照样要写）✅ {stable_ok}/{n}")
    if weird:
        print("\n  形态可疑的样本：")
        for w in weird:
            print("    · " + " ｜ ".join(w))


if __name__ == "__main__":
    main()
