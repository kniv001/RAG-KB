# -*- coding: utf-8 -*-
"""
把上下文"消解进检索"能不能成：直接拿**决策台账本身**当记忆库测。

设想的形态：上下文 = 钉死的约束 + 当前任务 + 最近一两轮；索引常驻（小）；记忆块按需召回。
这个形状在应用里已经有一半（树概览常驻 + 块按需），缺的是"工作记忆/流程/项目"那一半。

要测的不是"能不能记住"（太软），而是这套架构**唯一会烂掉的地方**：
库里同时躺着**被撤回的结论**和**它的更正**时，检索会不会把陈旧的当事实端出来。

天然素材：2026-09-17 那天把「因果恒 0」记成模型能力边界，当晚更正为「think:false + format
的产物」。更正的旧版本还在 git 里 —— 于是可以造一个**阳性对照**（D 组）：
故意把库冻结在更正之前，如果连它都测不出错，那 C 组测出来的"没事"就一文不值。

四组（同一批问题、同一模型、同一提示词骨架，只改"给了什么记忆"）：
  A 无记忆     —— 只有问题（底噪：靠预训练能猜多少）
  B 全量       —— 台账全部节点塞进上下文（上限对照）
  C 索引+召回  —— INDEX 常驻 + top-3 召回（**用户设想的形态**，库是现在的）
  D 索引+召回  —— 同上，但库来自 `git show <更正前>`（**应当答错**）

三个判据：
  ① 结论对不对     —— 关键短语命中（机械，但要读答案，别只看数字）
  ② 召回对不对     —— top-3 里有没有那个节点（纯机械）
  ③ 有没有引用陈旧的 —— 命中"撤回关键词"（D 组必须命中，否则判据无效）

用法：
  python tools/memory-context-probe.py --dry          # 只打印库与问题，不调模型（先验 harness）
  python tools/memory-context-probe.py [题数，默认 10]
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
CHAT, EMBED = "qwen3:4b", "bge-m3"
LEDGER = r"D:\vs\decisions\personal-rag-kb"
REPO = r"D:\vs"
# 更正发生在 29d13af4；它**之前**的那个版本就是"库里有陈旧结论"的状态
FIX_COMMIT = "29d13af4"
STALE_NODE = "2026-09-17-句子类型打标不如正则.md"
CORRECTION_NODE = "2026-09-17-不许想的模型.md"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_memory-test")
# 台账全文约 1.7 万 token，16384 装不下 B 组（"全量塞进去"这个对照本身）；
# 24576 是今天实测过的安全值（再往上 32768 会掉到 10 tok/s）
NUM_CTX = 24576

# 问题集：每条给「正确关键词」与「陈旧关键词」。关键词只作机械提示，判定要读答案。
QUESTIONS = [
    dict(q="命题改写为什么判负？",
         keys=["独有词", "覆盖", "84", "36"], stale=["命题比块好"]),
    dict(q="把 KV 放系统内存流式为什么行不通？",
         keys=["带宽", "PCIe", "10 tok"], stale=["显存不够", "模型不支持"]),
    dict(q="「因果恒 0」说明了什么？",
         keys=["think", "不许想", "测量", "8/11", "思考"],
         stale=["模型处理不了", "模型做不到", "能力边界", "确实判不出因果"]),
    dict(q="上下文窗口上限实测到多少？",
         keys=["24576", "28672", "32768"], stale=["10240", "16384 就是上限"]),
    dict(q="分块改成句子边界解决了什么问题？",
         keys=["75", "13", "半句"], stale=["没解决", "重复"]),
    dict(q="段检索为什么判死？",
         keys=["密度", "漏", "预算", "二选一"], stale=["块更好所以段无用"]),
    dict(q="结构化切分为什么放弃？",
         keys=["需求", "作者", "标题", "适配"], stale=["模型切不出来"]),
    dict(q="DP 选块为什么没有收益？",
         keys=["装得下", "预算", "候选"], stale=["语料太小"]),
    dict(q="今天最贵的教训是什么？",
         keys=["口子", "退化解", "测量", "哨兵"], stale=[]),
    dict(q="入库抓取环节改了什么？",
         keys=["Markdown", "标题", "h1"], stale=["没改", "仍是纯文本"]),
    dict(q="句子类型打标这层现在该怎么做？",
         keys=["正则", "零成本", "确定"], stale=["用模型", "模型做不到", "恒 0"]),
]

ANSWER_SYS = """你是这个项目的助手。只根据下面给你的材料回答，材料里没有的就直说没有。

要求：
- 直接给结论，再给一句依据
- 不要罗列材料，不要复述问题
- 三句话以内"""


def sh(args, cwd=REPO):
    # core.quotepath=false 必须给：默认情况下非 ASCII 文件名会被引号裹成 "\344\274\230..."，
    # 于是 endswith(".md") 全部落空 —— dry run 时"冻结库只剩 1 个节点"就是这个
    return subprocess.run([args[0], "-c", "core.quotepath=false"] + args[1:],
                          cwd=cwd, capture_output=True).stdout.decode("utf-8", "replace")


def load_store(commit=None):
    """读台账节点。commit 给了就从那一次提交里读（造陈旧库）。"""
    if commit is None:
        names = [f for f in os.listdir(LEDGER) if f.endswith(".md") and f != "INDEX.md"]
        store = {}
        for n in names:
            store[n] = io.open(os.path.join(LEDGER, n), encoding="utf-8").read()
        index = io.open(os.path.join(LEDGER, "INDEX.md"), encoding="utf-8").read()
        return store, index
    listing = sh(["git", "ls-tree", "--name-only", f"{commit}^", "decisions/personal-rag-kb/"])
    names = [os.path.basename(x.strip()) for x in listing.splitlines() if x.strip().endswith(".md")]
    store = {}
    for n in names:
        if n == "INDEX.md":
            continue
        store[n] = sh(["git", "show", f"{commit}^:decisions/personal-rag-kb/{n}"])
    index = sh(["git", "show", f"{commit}^:decisions/personal-rag-kb/INDEX.md"])
    return store, index


def post(path, body, timeout=300, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


def embed(texts):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]})["embeddings"])
    a = np.array(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def retrieve(store, index_vec, qvec, k=3):
    """召回：问题向量 vs 节点向量。节点文本 = 文件名 + 前 600 字。"""
    names = list(store)
    texts = [n + "\n" + store[n][:600] for n in names]
    v = embed(texts)
    sims = v @ qvec
    order = np.argsort(-sims)[:k]
    return [(names[i], float(sims[i])) for i in order]


def ask(question, memory):
    user = (memory + "\n\n【问题】" + question) if memory else question
    d = post("/api/chat", {"model": CHAT, "stream": False, "think": True,
                           "options": {"temperature": 0.1, "num_ctx": NUM_CTX},
                           "messages": [{"role": "system", "content": ANSWER_SYS},
                                        {"role": "user", "content": user}]})
    m = d.get("message", {})
    # prompt_eval_count 是真实的提示词 token 数 —— 把"装不装得下"从估算变成实测
    return (m.get("content") or "").strip(), len(m.get("thinking") or ""), int(d.get("prompt_eval_count") or 0)


def score(ans, keys, stale):
    hit = sum(1 for k in keys if k in ans)
    bad = sum(1 for k in stale if k in ans)
    return hit, bad


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    dry = "--dry" in sys.argv
    n_q = next((int(a) for a in sys.argv[1:] if a.isdigit()), len(QUESTIONS))
    qs = QUESTIONS[:n_q]

    now, index_now = load_store()
    old, index_old = load_store(FIX_COMMIT)
    print(f"库（现在）：{len(now)} 个节点，{sum(len(v) for v in now.values())} 字")
    print(f"库（冻结在 {FIX_COMMIT}^）：{len(old)} 个节点，{sum(len(v) for v in old.values())} 字")
    print(f"差集：多了 {sorted(set(now) - set(old))}　少了 {sorted(set(old) - set(now))}")
    print(f"陈旧库里那条的文本长度 {len(old.get(STALE_NODE, ''))}，"
          f"现在那条 {len(now.get(STALE_NODE, ''))}")
    if CORRECTION_NODE in old:
        print("！陈旧库里竟然有更正节点 —— D 组对照无效，先查 FIX_COMMIT")
        return

    full_now = "\n\n".join(f"### {k}\n{v}" for k, v in now.items())
    full_old = "\n\n".join(f"### {k}\n{v}" for k, v in old.items())
    print(f"全量条件的体积：现在 {len(full_now)} 字 ≈ {len(full_now)//1.5:.0f} token 量级"
          f"（窗口 16384，需装得下）")

    if dry:
        print("\n—— 问题集 ——")
        for i, q in enumerate(qs, 1):
            print(f"  {i:>2}. {q['q']}")
        print("\n—— 陈旧库那一版的开头（确认它确实把「因果恒 0」当结论）——")
        print("\n".join(old.get(STALE_NODE, "(缺)").splitlines()[:8]))
        print("\n（--dry 到此，未调模型）")
        return

    if "--probe-b" in sys.argv:
        # B 组单发试装：先确认"全量塞进去"真的装得下，再决定要不要跑整轮
        ans, th, pt = ask(qs[0]["q"], "【项目台账全文】\n" + full_now)
        print(f"\nB 组试装：提示词 {pt} token（窗口 {NUM_CTX}，阈值 {int(NUM_CTX*0.92)}）"
              f"　思考 {th} 字")
        print(f"回答：{ans}")
        return

    os.makedirs(OUT, exist_ok=True)
    qv = embed([q["q"] for q in qs])
    idx_v = embed([index_now])[0]

    results = []
    for tag, store, index in [("A 无记忆", None, None),
                              ("B 全量(上限)", None, None),
                              ("C 索引+召回", now, index_now),
                              ("C' 只给索引", now, index_now),
                              ("D 陈旧库", old, index_old)]:
        print(f"\n===== {tag} =====", flush=True)
        for i, q in enumerate(qs, 1):
            if tag.startswith("A"):
                mem, recalled = "", []
            elif tag.startswith("B"):
                mem, recalled = "【项目台账全文】\n" + full_now, []
            elif tag.startswith("C'"):
                # 只有索引、不召回正文 —— 量"索引那一半"独自能承载多少
                mem, recalled = "【项目记忆索引】\n" + index.strip(), []
            else:
                recalled = retrieve(store, None, qv[i - 1], k=3)
                blocks = "\n\n".join(f"### {n}\n{store[n]}" for n, _ in recalled)
                mem = "【项目记忆索引】\n" + index.strip() + "\n\n【召回到的记忆】\n" + blocks
            ans, th, pt = ask(q["q"], mem)
            hit, bad = score(ans, q["keys"], q["stale"])
            results.append(dict(cond=tag, i=i, q=q["q"], ans=ans, think=th, prompt_tok=pt,
                                hit=hit, bad=bad, recalled=[n for n, _ in recalled]))
            over = "  ⚠超窗口" if pt > NUM_CTX * 0.92 else ""
            flag = "陈旧!" if bad else ""
            print(f"  {i:>2}. 命中 {hit}/{len(q['keys'])}　提示词 {pt} token{over}　{flag}"
                  f"　{ans[:64]!r}", flush=True)
            if recalled:
                print(f"      召回：{[n[:34] for n, _ in recalled]}", flush=True)

    json.dump(results, io.open(os.path.join(OUT, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print("\n—— 汇总 ——")
    print(f"{'条件':<14}{'命中/总':>10}{'陈旧命中':>10}{'均提示词':>10}")
    for tag in ["A 无记忆", "B 全量(上限)", "C 索引+召回", "C' 只给索引", "D 陈旧库"]:
        rs = [r for r in results if r["cond"] == tag]
        if not rs:
            continue
        h = sum(r["hit"] for r in rs)
        t = sum(len([k for k in next(x["keys"] for x in qs if x["q"] == r["q"])])
                for r in rs)
        pt = int(sum(r["prompt_tok"] for r in rs) / len(rs))
        print(f"{tag:<14}{h:>6}/{t:<4}{sum(r['bad'] for r in rs):>9}{pt:>10}")
    print(f"\n明细已存 {os.path.join(OUT, 'results.json')}（判定要读答案，别只看数字）")


if __name__ == "__main__":
    main()
