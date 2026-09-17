# -*- coding: utf-8 -*-
"""
滑窗边界信号：用「局部比较」找结构，全程不生成内容。

思路（用户提的「卷积」）：把文档当成一个序列，在每个位置做一次**局部**运算，
产出一条「这里像不像边界」的信号，再在信号上取峰。相比前几种方案的关键区别：

  · 没有任何「生成」—— 用向量模型，不问模型要答案
  · 因此没有「形状合法但内容空洞」的退化解：信号是平的能直接测出来（方差 / 峰谷比）
  · 判定是局部的（相邻窗口比一比），而局部判断正是模型/向量做得住的那个规模

判据这一次是**量化的**：新抓的两篇文档自带标题层级（一篇 59 个标题），
标题位置就是标准答案 —— 算检出边界的精确率 / 召回率，不用肉眼读。

用法：python tools/boundary-signal-probe.py [文档id] [窗口段落数]
"""
import io
import json
import os
import re
import sys
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434/api/embed"
EMBED = "bge-m3"
STORE = r"D:\vs\rag-kb\data\uploads"
HERE = os.path.dirname(os.path.abspath(__file__))
CJK = re.compile(r"[\u4e00-\u9fff]")
CODEY = re.compile(r"[{}();=<>\[\]]|^\s*(public|private|import|class|def|SELECT|while|for|if)\b", re.MULTILINE)


def embed_all(texts, tag=""):
    out = []
    for i in range(0, len(texts), 16):
        body = json.dumps({"model": EMBED, "input": texts[i:i + 16]}).encode("utf-8")
        req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.load(r)["embeddings"])
        if tag and i % 1600 == 0:
            print(f"    嵌入 {tag} {i}/{len(texts)}", flush=True)
    a = np.array(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def paragraphs(path):
    """按空行切段；标题行单独成段并标记"""
    text = io.open(path, encoding="utf-8", errors="replace").read()
    out = []
    for raw in re.split(r"\n\s*\n", text):
        t = raw.strip()
        if not t:
            continue
        out.append({"text": t, "head": bool(re.match(r"^#{1,6}\s", t))})
    return out


def codey(seg):
    cjk = len(CJK.findall(seg))
    return (cjk / max(len(seg), 1) < 0.35) or bool(CODEY.search(seg))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    doc = sys.argv[1] if len(sys.argv) > 1 else "d08e262271ee"
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    paras = paragraphs(os.path.join(STORE, doc + ".md"))
    heads = [i for i, p in enumerate(paras) if p["head"]]
    print(f"文档 {doc}：{len(paras)} 段，其中标题 {len(heads)} 个（这就是标准答案）")
    print(f"段长：中位 {int(np.median([len(p['text']) for p in paras]))} 字，"
          f"代码味段 {sum(1 for p in paras if codey(p['text']))} 个\n")

    X = embed_all([p["text"] for p in paras], "段")

    # 卷积式信号：每个位置算「段内相似度 − 跨边界相似度」（TextTiling 的 depth 分）
    n = len(X)
    depth = np.zeros(n)
    for i in range(1, n):
        L = X[max(0, i - w):i]
        R = X[i:min(n, i + w)]
        if len(L) == 0 or len(R) == 0:
            continue
        within = (L @ L.T).mean() if len(L) > 1 else 1.0
        within_r = (R @ R.T).mean() if len(R) > 1 else 1.0
        cross = (L @ R.T).mean()
        depth[i] = (within + within_r) / 2 - cross

    print(f"—— 信号（窗口 {w} 段）——")
    print(f"  深度分：中位 {np.median(depth):.3f}，最大 {depth.max():.3f}，"
          f"标准差 {depth.std():.3f}")
    flat = depth.std() < 0.01
    print(f"  信号是否平坦（=这份文档没有结构）：{'⚠️ 是' if flat else '否'}")

    # 取峰：高于「中位 + 1.5 倍标准差」的局部极大
    thr = np.median(depth) + 1.5 * depth.std()
    peaks = [i for i in range(1, n) if depth[i] >= thr and depth[i] >= depth[max(0, i - 2):i + 3].max()]
    # 太近的峰合并（窗口 w 内只留最高的）
    merged = []
    for i in peaks:
        if merged and i - merged[-1] <= w:
            if depth[i] > depth[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)
    peaks = merged

    print(f"\n—— 检出边界 {len(peaks)} 个（阈值 {thr:.3f}）——")
    gt = set(heads)
    hit = [i for i in peaks if i in gt]
    prec = len(hit) / max(len(peaks), 1)
    rec = len(hit) / max(len(gt), 1)
    print(f"  与标题位置比对：精确率 {100*prec:.0f}%（{len(hit)}/{len(peaks)}）　"
          f"召回率 {100*rec:.0f}%（{len(hit)}/{len(gt)}）")
    # 命中或错过 1 段内也算（段落切分与标题行不完全对齐）
    near = [i for i in peaks if any(abs(i - g) <= 1 for g in gt)]
    print(f"  容差 ±1 段：精确率 {100*len(near)/max(len(peaks),1):.0f}%　"
          f"召回率 {100*len([g for g in gt if any(abs(g-p)<=1 for p in peaks)])/max(len(gt),1):.0f}%")

    bad = [i for i in peaks if i > 0 and codey(paras[i-1]["text"]) and codey(paras[i]["text"])]
    print(f"  其中两侧都是代码味的（切在代码里）：{len(bad)}/{len(peaks)}")

    # 阈值是一根旋钮，拍一个值下结论不成立 —— 扫一遍看精确率/召回率曲线
    print("\n—— 阈值扫描（看这条信号的上限在哪）——")
    print(f"{'阈值':>8}{'检出':>7}{'精确率':>9}{'召回率':>9}{'F1':>8}{'切在代码里':>11}")
    for k in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
        t = np.median(depth) + k * depth.std()
        pk = [i for i in range(1, n) if depth[i] >= t and depth[i] >= depth[max(0, i - 2):i + 3].max()]
        m = []
        for i in pk:
            if m and i - m[-1] <= w:
                if depth[i] > depth[m[-1]]:
                    m[-1] = i
            else:
                m.append(i)
        pk = m
        h = len([i for i in pk if any(abs(i - g) <= 1 for g in gt)])
        p = h / max(len(pk), 1)
        r = h / max(len(gt), 1)
        f1 = 2 * p * r / max(p + r, 1e-9)
        b = len([i for i in pk if i > 0 and codey(paras[i-1]["text"]) and codey(paras[i]["text"])])
        print(f"{t:>8.3f}{len(pk):>7}{100*p:>8.0f}%{100*r:>8.0f}%{f1:>8.2f}{b:>11}")
    print("\n  检出的前 6 个边界：")
    for i in peaks[:6]:
        mark = "✅ 是标题" if i in gt else ("≈ 标题附近" if any(abs(i-g) <= 1 for g in gt) else "✗ 非标题")
        print(f"    段{i}  {mark}　前: …{paras[i-1]['text'][-30:].strip()}")
        print(f"          后: {paras[i]['text'][:40].strip()}…")


if __name__ == "__main__":
    main()
