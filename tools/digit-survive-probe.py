# -*- coding: utf-8 -*-
"""
**数字在摘要合并里活不活得下来** —— 逐字判据，跑 N 次看打滑率。

为什么另起一台（2026-09-20）：`digit-fidelity-probe` 测的是 600 的四档任务，
`digit-garble-probe` 测的是"冒号串位"这个假设。而今天在**长列表的合并**里
直接看到 `24576 → 2:4576` —— 冒号插在**第 1 位之后**（不是末位），
和台账里记的「末位被吃」是**两种不同的糊法**。

台账原来记的是「数字糊化只在 q8 下出现（10/10 vs 0/10）」，
而这一条是在 **q4_0** 下看到的 ⇒ 那个结论要么是数字特异的、要么是**打滑**没被 10 次抓到。
所以这台机器的判据要**逐字**、要**分数字**、要**跑够次数**。

用法：python tools/digit-survive-probe.py [次数，默认 12] [模型]
"""
import importlib.util
import io
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

# 已有摘要（长列表，接近生产里涨起来的样子）+ 本轮新增对话
OLD = """分块粒度：— → 450 字
用户偏好：— → 表格而非选项式
向量模型：— → bge-m3
窗口上限：— → 24576
否决方向：DP 选块（候选装得下，收益为 0）
摘要窗口：— → 20 条
裁剪顺序：— → 摘要与召回片段共进退
嵌入文本：— → 块文本前面拼一行语境行
云端仓库：— → 不暴露网址
分块方式：— → 按句子边界
PDF 解析：— → PDFBox
向量维度：— → 1024
检索融合：— → RRF
摘要写法：— → 一行一条
KV 量化：— → q4_0
认证：— → 双令牌
日志保留：— → 10240 行
分片数：— → 16384"""

FRESH = ("用户：再确认一下，嵌入用的文本前面那行语境行，是不进提示词的对吧。\n"
         "助手：对，只进索引，不进提示词，查询侧零开销。\n"
         "用户：好，这条别忘了。\n"
         "助手：已记住。")

# 要盯的数字：**各有各的作用**
# 这几个数字**都在 OLD 里**（不在的话"没存活"是必然的，不是打滑 —— 第一版就踩了：
# 10240 没写进输入却拿去判存活，测出来 0/12，纯属探针自己的错）。
NUMS = ["24576", "10240", "16384", "450", "1024", "20"]


def post(system, user, model, timeout=180):
    body = {"model": model, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.2, "num_ctx": 8192},
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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    model = sys.argv[2] if len(sys.argv) > 2 else "qwen3:4b"
    user = f"已有条目：\n{OLD}\n\n新增对话：\n{FRESH}"
    print(f"模型 {model}　跑 {n} 次　看这 {len(NUMS)} 个数字**逐字**活几次\n")

    alive = {x: 0 for x in NUMS}
    garbled = {x: [] for x in NUMS}
    for i in range(1, n + 1):
        raw = post(PROMPT, user, model)
        try:
            items = [str(x) for x in (json.loads(raw).get("items") or [])]
        except Exception:
            items = []
        joined = "\n".join(items)
        line = []
        for num in NUMS:
            if num in joined:
                alive[num] += 1
                line.append(f"{num}=✅")
            else:
                # 找出被糊成了什么：同一条里最像的片段
                m = re.search(rf"{num[0]}[^一-鿿]{{0,3}}?{num[1:]}", joined)
                garbled[num].append(m.group(0) if m else "(整条没了)")
                line.append(f"{num}=✗")
        print(f"  {i:>2}. " + "  ".join(line), flush=True)

    print(f"\n{'数字':>8}{'逐字存活':>10}   糊成的样子（去重）")
    for num in NUMS:
        ratio = f"{alive[num]}/{n}"
        kinds = sorted(set(garbled[num]))
        print(f"{num:>8}{ratio:>10}   {'；'.join(kinds[:4]) if kinds else '—'}")
    bad = sum(1 for x in NUMS if alive[x] < n)
    print(f"\n{'⚠ 有数字会打滑' if bad else '✅ 全部逐字保住'} —— "
          f"打滑的数字 {bad}/{len(NUMS)} 个；总打滑 {sum(n - alive[x] for x in NUMS)}/{n*len(NUMS)} 次")


if __name__ == "__main__":
    main()
