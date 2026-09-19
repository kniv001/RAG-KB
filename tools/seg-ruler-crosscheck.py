# -*- coding: utf-8 -*-
"""
尺子的**第二标注者复核**：换一个家族的模型（llama3.1:8b）逐点判真假。

为什么不用"让它重新标一遍"：那会给它自己的退化留位置（今天见过太多）。
改成**逐点问**：对每个已标位置单独问「这里是不是一个新话题的开始」，
而且**混进对照点** —— 标成 optional 的、落在 forbidden 里的，正确答案都是「不是」。
于是两个数一起出来：**同意率**（该"是"的答"是"）与**虚报率**（该"不是"的答"是"）。

提示词里**不出现任何具体编号当示例**（今天实测：示例值会被原样抄走）。

用法：python tools/seg-ruler-crosscheck.py [模型，默认 llama3.1:8b]
"""
import io
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CFG = os.path.join(HERE, "seg-ruler-annotation.json")
SEGS = os.path.join(HERE, "..", "data", "_bintree-segs.json")
JUDGE = sys.argv[1] if len(sys.argv) > 1 else "llama3.1:8b"

PROMPT = """下面给出文档里连续若干段（按段号排列）。请判断**指定的那一段**是不是一个新话题的开始。

判据：「从这里开始，讲的东西明显换了一件」——比如从小节标题换到另一个小节、从正文换到代码配置、
从这篇文章换到另一篇文章。仅仅"换个说法继续讲同一件事"不算。

只输出 JSON，字段 ok，值是 true 或 false。不要解释。"""

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def ask(segs, lo, hi, target, focus=0):
    """focus>0 时只给目标段前后各 focus 段 —— 整窗 25 段对标注者不公平（实测漏掉明晃晃的小节标题）。"""
    if focus:
        a = max(lo, target - 1 - focus)
        b = min(hi, target + focus)
    else:
        a, b = lo, hi
    body = {"model": JUDGE, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0.1, "num_ctx": 8192},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content":
                          "\n".join(f"{i+1}. {segs[i].strip()[:160]}" for i in range(a, b))
                          + f"\n\n要判断的是：第 {target} 段"}]}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        raw = json.load(r).get("message", {}).get("content", "")
    try:
        return bool(json.loads(raw).get("ok"))
    except Exception:
        return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ann = json.load(io.open(CFG, encoding="utf-8"))
    segs = json.load(io.open(SEGS, encoding="utf-8"))["segs"]
    print(f"第二标注者 {JUDGE}　逐点复核（同意率 / 虚报率）\n")

    focus = int(os.environ.get("KB_FOCUS", "3"))
    tp = fn = fp = tn = 0
    rows = []
    for w in ann["windows"]:
        lo, hi = w["lo"], w["hi"]
        yes_pts = [m["at"] for m in w["must"]][:4]
        # 反例**只能是**「落在禁切区里的位置」——
        # 第一版拿 optional 当反例是设计错误：可切点按定义就是"切了也说得通"，
        # 一个合理的标注者答"是"不算虚报（实测 7 次虚报里 6 次是这么来的）。
        no_pts = []
        for f in w["forbidden"][:2]:
            no_pts.append(min(f["lo"] + 1, f["hi"]))
        for t in yes_pts:
            got = ask(segs, lo, hi, t, focus)
            tp += got is True
            fn += got is not True
            rows.append((t, True, got))
        for t in no_pts:
            got = ask(segs, lo, hi, t, focus)
            fp += got is True
            tn += got is not True
            rows.append((t, False, got))
        print(f"  窗口 {lo+1}-{hi} 完成", flush=True)

    print(f"\n—— 结果 ——")
    print(f"  该「是」的 {tp+fn} 个：答是 {tp}（同意率 {100*tp/max(tp+fn,1):.0f}%）")
    print(f"  该「不是」的 {fp+tn} 个：答是 {fp}（虚报率 {100*fp/max(fp+tn,1):.0f}%）")
    print("\n  逐点：")
    for t, want, got in rows:
        mark = "✅" if (got is True) == want else "❌"
        print(f"    {mark} 第{t}段　应「{'是' if want else '不是'}」　实得 "
              f"{'是' if got is True else ('不是' if got is False else '解析失败')}")


if __name__ == "__main__":
    main()
