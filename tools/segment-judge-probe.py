# -*- coding: utf-8 -*-
"""
用「读概率」救**分段**：逐位置问「这一段是不是一个新话题的开始？」→ 读**是/否的概率**。

为什么这条最像能救：结构化切分当年四条路全判负，死因是**退化解**（不切 / 凑数 / 恒选一侧）——
**那些退化全长在"生成"上**。这个形态里：
  · **没有"不切"这个出口**（每个位置都必须给出一个概率）
  · **没有"凑数"**（不生成章段数这种数字）
  · 阈值可以扫，而**阈值本身就是"多细"的旋钮**（不需要模型理解"每章 3~12 段"）

判据用今天做好的**分段尺子**（`seg-ruler.py`：must/optional/forbidden 三类标注 + WindowDiff）。
对照：手工 0.00、**指认式+代码后置 0.32**、每 8 段 0.50、每 5 段 0.57、全不切 0.62、每 3 段 0.76。

用法：python tools/segment-judge-probe.py [模型，默认 qwen3:4b]
"""
import importlib.util
import io
import json
import math
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = os.environ.get("KB_JUDGE_MODEL", "qwen3:4b")
SEGS = os.path.join(HERE, "..", "data", "_bintree-segs.json")

# few-shot：形状必须与任务一致（"前文 + 这一段 + 是不是新话题开始"）
FEWSHOT = (
    "前文：\n- 漏桶算法按照固定速率流出水滴。\n"
    "这一段：\n- 令牌桶算法按照固定速率往桶里放令牌，桶满了就丢弃。\n"
    "这一段是不是一个新话题的开始？\n答：否\n"
    "前文：\n- 令牌桶算法按照固定速率往桶里放令牌。\n"
    "这一段：\n- 四、使用 Semaphore 进行并发流控。Java 并发库的 Semaphore 可以控制同时访问的个数。\n"
    "这一段是不是一个新话题的开始？\n答：是\n")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def judge_boundary(prev, cur, topn=20, timeout=300):
    p = FEWSHOT + (f"前文：\n{prev}\n这一段：\n{cur}\n这一段是不是一个新话题的开始？\n答：")
    body = {"model": CHAT, "prompt": p, "raw": True, "stream": False, "think": False,
            "logprobs": True, "top_logprobs": topn,
            "options": {"temperature": 0, "num_predict": 1, "num_ctx": 4096}}
    req = urllib.request.Request(OLLAMA + "/api/generate",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    lp = (d.get("logprobs") or [{}])[0].get("top_logprobs") or []
    m = {}
    for t in lp:
        for k in {t["token"], t["token"].strip()}:
            m[k] = max(m.get(k, float("-inf")), t["logprob"])
    a, b = m.get("是"), m.get("否")
    return (math.exp(a) / (math.exp(a) + math.exp(b))) if (a is not None and b is not None) else None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sr = load("seg-ruler")
    ann = sr.load()
    segs = json.load(io.open(SEGS, encoding="utf-8"))["segs"]

    # 每个窗口内的每个位置算一个 P(是)
    scores = {}
    for w in ann["windows"]:
        lo, hi = w["lo"], w["hi"]
        for i in range(lo + 1, hi + 1):
            prev = "\n".join(f"- {segs[j].strip()[:90]}" for j in range(max(lo, i - 3), i))
            p = judge_boundary(prev or "（无）", f"- {segs[i-1].strip()[:120]}")
            scores[i] = p
        print(f"  窗口 {lo+1}-{hi} 完成（{hi-lo-1} 个位置）", flush=True)

    print(f"\n{'阈值':>7}{'切点数':>7}{'WD':>8}{'必切召回':>10}{'误切':>6}")
    best = None
    for th in (0.5, 0.7, 0.9, 0.95, 0.99):
        bounds = [i for i, p in scores.items() if p is not None and p >= th]
        r = sr.score(bounds, ann)
        print(f"{th:>7.2f}{len(bounds):>7}{r['WD']:>8.2f}{100*r['R']:>9.0f}%{r['误切']:>6}")
        if best is None or r["WD"] < best[1]:
            best = (th, r["WD"], len(bounds))
    print(f"\n最好：阈值 {best[0]} → WD {best[1]:.2f}（{best[2]} 刀）")
    print("对照：手工 0.00　**指认式+后置 0.32**　每 8 段 0.50　每 5 段 0.57　全不切 0.62　每 3 段 0.76")
    io.open(os.path.join(HERE, "_segjudge.json"), "w", encoding="utf-8").write(
        json.dumps(scores, ensure_ascii=False))


if __name__ == "__main__":
    main()
