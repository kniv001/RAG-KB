# -*- coding: utf-8 -*-
"""
索引写成「结论式」还是「变化式」：只给索引、不给正文，同一批问题对打。

背景（见 decisions/personal-rag-kb/2026-09-18-记忆方向：出发点、已落地、待验证.md）：
- 只给 22 行**结论式**索引（2019 token）就答对 10/11 —— 索引那一行承载大半
- 但阳性对照证明：**被撤回的结论会被当事实端出**（库里两条状态平权地摆着，模型挑了旧的）

设想（用户提的）：索引那一行不写「现在是什么」，写「**从什么变成了什么**」。
- 好处：谁是当前的由**顺序**决定（箭头右边），不用模型做内容判断
- 代价：读者要会折叠；检索必须保证最新那条被召回

两个条件，输入逐字相同、只换索引文本：
  状态式 = decisions/personal-rag-kb/INDEX.md（结论式一行一条）
  变化式 = tools/memory-index-change-form.txt（X：曾 → 现（因为…））

判据：与 [[2026-09-18-记忆当检索：索引那一行承载大半]] 同一套关键词，
**但必须读答案** —— 机械判据在否定句上会反转（"而非模型能力边界"会命中"能力边界"）。
重点看题3与题11（两道含撤回的题）。

用法：python tools/memory-index-shape-probe.py [题数，默认 11]
"""
import io
import json
import os
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_INDEX = r"D:\vs\decisions\personal-rag-kb\INDEX.md"
CHANGE_INDEX = os.path.join(HERE, "memory-index-change-form.txt")
OUT = os.path.join(HERE, "..", "data", "_memory-test")
NUM_CTX = 24576

QUESTIONS = [
    dict(q="命题改写为什么判负？",
         keys=["独有词", "覆盖", "84", "36"], stale=[]),
    dict(q="把 KV 放系统内存流式为什么行不通？",
         keys=["带宽", "PCIe", "10 tok"], stale=[]),
    dict(q="「因果恒 0」说明了什么？",
         keys=["think", "不许想", "测量", "8/11", "思考"],
         # 注：不能把"能力边界"放进陈旧词 —— 正确答案是"**而非**模型能力边界"，
         # 否定句会让机械判据反转（今天栽过）。这类一律靠读。
         stale=["模型处理不了", "模型做不到", "确实判不出因果"]),
    dict(q="上下文窗口上限实测到多少？",
         keys=["24576", "28672", "32768"], stale=["10240 就是上限"]),
    dict(q="分块改成句子边界解决了什么问题？",
         keys=["75", "13", "半句"], stale=[]),
    dict(q="段检索为什么判死？",
         keys=["密度", "漏", "预算", "二选一"], stale=[]),
    dict(q="结构化切分为什么放弃？",
         keys=["需求", "作者", "标题", "适配"], stale=[]),
    dict(q="DP 选块为什么没有收益？",
         keys=["装得下", "预算", "候选"], stale=[]),
    dict(q="今天最贵的教训是什么？",
         keys=["口子", "退化解", "测量", "哨兵"], stale=[]),
    dict(q="入库抓取环节改了什么？",
         keys=["Markdown", "标题", "h1"], stale=[]),
    dict(q="句子类型打标这层现在该怎么做？",
         keys=["正则", "零成本", "确定"], stale=["模型做不到", "恒 0"]),
]

ANSWER_SYS = """你是这个项目的助手。只根据下面给你的材料回答，材料里没有的就直说没有。

要求：
- 直接给结论，再给一句依据
- 不要罗列材料，不要复述问题
- 三句话以内"""


def post(body, timeout=300, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


def ask(question, index_text):
    d = post({"model": CHAT, "stream": False, "think": True,
              "options": {"temperature": 0.1, "num_ctx": NUM_CTX},
              "messages": [{"role": "system", "content": ANSWER_SYS},
                           {"role": "user",
                            "content": "【项目记忆索引】\n" + index_text.strip()
                                       + "\n\n【问题】" + question}]})
    m = d.get("message", {})
    return (m.get("content") or "").strip(), int(d.get("prompt_eval_count") or 0)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = next((int(a) for a in sys.argv[1:] if a.isdigit()), len(QUESTIONS))
    qs = QUESTIONS[:n]
    os.makedirs(OUT, exist_ok=True)

    conds = [("状态式（结论式一行）", io.open(STATE_INDEX, encoding="utf-8").read()),
             ("变化式（曾→现）", io.open(CHANGE_INDEX, encoding="utf-8").read())]
    for tag, txt in conds:
        print(f"{tag}：{len(txt)} 字，{len([l for l in txt.splitlines() if l.strip().startswith('-')])} 条\n")

    results, readout = [], []
    for tag, idx in conds:
        print(f"===== {tag} =====", flush=True)
        for i, q in enumerate(qs, 1):
            ans, pt = ask(q["q"], idx)
            hit = sum(1 for k in q["keys"] if k in ans)
            bad = sum(1 for k in q["stale"] if k in ans)
            results.append(dict(cond=tag, i=i, q=q["q"], ans=ans, hit=hit, bad=bad, pt=pt))
            print(f"  {i:>2}. 命中 {hit}/{len(q['keys'])}  提示词 {pt}"
                  f"{'  陈旧!' if bad else ''}", flush=True)
            readout.append(f"\n题{i}：{q['q']}\n  [{tag}] {ans}")
        print(flush=True)

    json.dump(results, io.open(os.path.join(OUT, "index-shape.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    io.open(os.path.join(OUT, "index-shape-readout.txt"), "w", encoding="utf-8").write(
        "\n".join(readout))

    print("—— 汇总（判定要读答案）——")
    print(f"{'条件':<20}{'命中/总':>10}{'机械判陈旧':>12}")
    for tag, _ in conds:
        rs = [r for r in results if r["cond"] == tag]
        h = sum(r["hit"] for r in rs)
        t = sum(len(next(x["keys"] for x in qs if x["q"] == r["q"])) for r in rs)
        print(f"{tag:<20}{h:>6}/{t:<4}{sum(r['bad'] for r in rs):>10}")
    print(f"\n逐条答案：{os.path.join(OUT, 'index-shape-readout.txt')}")


if __name__ == "__main__":
    main()
