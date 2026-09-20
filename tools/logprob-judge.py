# -*- coding: utf-8 -*-
"""
**本地判断器（Jev 式）**：不生成，直接读「是 / 否」两个 token 的概率。

为什么这条路值得走：今天一整天的判断类失败（抄示例值 / 全 false / 恒选一侧 / 拒答填 0）
**全都长在"生成"这条路上** —— `format` 把第一个 token 钉成 `{`，模型只好交最安全的合法输出。
Jev 那类模型的机制是**不生成、读候选 token 的 logits**。

本机的关键发现（2026-09-20）：**必须 `raw: true`**。
  · `/api/generate` 默认会套聊天模板 ⇒ 首 token 是闲聊开场白（「首先」「嗯」），
    前 20 名里**根本没有**「是」「否」
  · `raw: true` + 两条 few-shot ⇒ 首 token 就是答案，且 top5 是 ['是','否',' 是','yes','Yes']
概率差是两级的（判对时 P 常是 1.000 / 0.021），**概率本身就是置信度**。

用法：
  python tools/logprob-judge.py sanity      # 抽检一批判断（含正反问）
  python tools/logprob-judge.py rerank      # 拿它当精排器，量多跳全中率
"""
import io
import json
import math
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = os.environ.get("KB_JUDGE_MODEL", "qwen3:4b")

# few-shot：确立"答：是/否"的续写格式。**不给选项字母、不给 JSON** —— 答案就是一个汉字 token。
FEWSHOT = ("问：RDB 是某一时刻的全量快照。这句讲的是 Redis 持久化吗？\n答：是\n"
           "问：Spring 的 Bean 默认是单例。这句讲的是 Redis 持久化吗？\n答：否\n")


def judge(question, fewshot=FEWSHOT, topn=20, timeout=300):
    """返回 (P(是), 是/否 的 logprob, top5 token)"""
    prompt = fewshot + f"问：{question}\n答："
    body = {"model": CHAT, "prompt": prompt, "raw": True, "stream": False, "think": False,
            "logprobs": True, "top_logprobs": topn,
            "options": {"temperature": 0, "num_predict": 1, "num_ctx": 2048}}
    req = urllib.request.Request(OLLAMA + "/api/generate",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    lp = (d.get("logprobs") or [{}])[0].get("top_logprobs") or []
    # **别 strip 后再进字典**：'是' 与 ' 是'（带空格）会撞进同一个键，
    # 后写入的低概率变体把真值覆盖掉（踩过：top1 明明是「是」，算出来的 P(是) 却是 0.166）。
    # 规则：精确名优先；同名变体取**最大** logprob。
    m = {}
    for t in lp:
        for key in {t["token"], t["token"].strip()}:
            m[key] = max(m.get(key, float("-inf")), t["logprob"])
    a, b = m.get("是"), m.get("否")
    if a is None or b is None:
        return None, (a, b), [t["token"] for t in lp[:5]]
    return math.exp(a) / (math.exp(a) + math.exp(b)), (a, b), [t["token"] for t in lp[:5]]


# **精排要用自己形状的 few-shot** —— 拿"这句讲的是 X 吗"那套去教"这段能回答这个问题吗"，
# 形状不匹配，靶子会掉到 16~17 名（实测）；换成同形状的示范后升到 1~5 名。
RERANK_FEWSHOT = (
    "问：Redis 挂了重启后数据还在吗？\n片段：RDB 是某一时刻的全量快照，AOF 记录每一条写命令。\n"
    "这段能直接回答上面的问题吗？\n答：是\n"
    "问：Redis 挂了重启后数据还在吗？\n片段：Kubernetes 调度器先过滤节点再打分。\n"
    "这段能直接回答上面的问题吗？\n答：否\n")


def judge_relevance(question, snippet, topn=20, timeout=300):
    """精排用的二值判断（形状与 FEWSHOT 不同，必须换示范）。"""
    prompt = RERANK_FEWSHOT + (f"问：{question}\n片段：{snippet[:300]}\n"
                              "这段能直接回答上面的问题吗？\n答：")
    body = {"model": CHAT, "prompt": prompt, "raw": True, "stream": False, "think": False,
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


def sanity():
    items = [
        ("真", "RDB 是某一时刻的全量快照。这句讲的是 Redis 持久化吗？", True),
        ("假", "Spring 的 Bean 默认是单例。这句讲的是 Redis 持久化吗？", False),
        ("真", "AOF 会记录每一条写命令。这句讲的是 Redis 持久化吗？", True),
        ("假", "G1 把堆划分成等大的 Region。这句讲的是 Redis 持久化吗？", False),
        ("真", "bgsave 靠 fork 子进程写快照。这句讲的是 Redis 持久化吗？", True),
        ("假", "HTTP/2 支持多路复用。这句讲的是 Redis 持久化吗？", False),
        # **正反问**：同一件事，一次问「是」一次问「否」—— 校准差的模型会两个都答"是"
        ("反问-该否", "RDB 是某一时刻的全量快照。这句**不是**讲 Redis 持久化，对吗？", False),
        ("反问-该是", "Spring 的 Bean 默认是单例。这句**不是**讲 Redis 持久化，对吗？", True),
    ]
    ok = 0
    print(f"{'项':<10}{'期望':>5}{'P(是)':>9}{'判':>5}   top3")
    for tag, q, truth in items:
        p, (a, b), top = judge(q)
        if p is None:
            print(f"{tag:<10}{str(truth):>5}{'缺':>9}{'—':>5}   {top}")
            continue
        good = (p > 0.5) == truth
        ok += good
        print(f"{tag:<10}{str(truth):>5}{p:>9.3f}{'✅' if good else '❌':>5}   {top[:3]}")
    print(f"\n判对 {ok}/{len(items)}")


def rerank(topk_pool=16, take=12):
    """把判断器当精排器：池子里逐候选问「这段能回答这个问题吗」，按 P(是) 排序取前 take。"""
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    live = json.load(io.open(os.path.join(HERE, "_multihop-live.json"), encoding="utf-8"))
    rows = psql_rows("SELECT c.id, coalesce(c.ctx,''), c.content, c.seq, d.name FROM chunks c "
                     "JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    ids = [r[0] for r in rows]
    texts = [(r[1] + "\n" + r[2]) if r[1] else r[2] for r in rows]
    bodies = [r[2] for r in rows]
    meta = [(r[3], r[4]) for r in rows]
    vecs = json.load(io.open(os.path.join(HERE, "_corpus_vecs.json"), encoding="utf-8"))["vecs"]
    norm = lambda s: re.sub(r"\s+", "", s)

    def groups(case):
        gs = []
        for t in case["targets"]:
            i = next((k for k, m in enumerate(meta)
                      if m[1] == case["doc"] and m[0] == str(t["seq"])), None)
            if i is None:
                gs.append([]); continue
            key = norm(bodies[i])[:40]
            gs.append([k for k, m in enumerate(meta)
                       if m[1] == case["doc"] and norm(bodies[k]).startswith(key[:20])] or [i])
        return gs

    def emb(qs):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": qs}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.load(r)["embeddings"]

    def cos(a, b):
        s = sum(x * y for x, y in zip(a, b))
        return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)

    allq = sorted({q for rec in live for q in (rec.get("queries") or [rec["q"]])})
    qv = {}
    for i in range(0, len(allq), 8):
        for q, v in zip(allq[i:i + 8], emb(allq[i:i + 8])):
            qv[q] = v

    base_ok = rr_ok = 0
    n = 0
    for case, rec in zip(cfg["cases"], live):
        gs = groups(case)
        scored = {}
        for q in (rec.get("queries") or [rec["q"]]):
            for rank, (c, j) in enumerate(sorted(((cos(qv[q], v), j) for j, v in enumerate(vecs)),
                                                 reverse=True)[:topk_pool]):
                scored[j] = scored.get(j, 0) + 1 / (60 + rank)
        pool = [j for j, _ in sorted(scored.items(), key=lambda x: -x[1])][:24]
        base = set(pool[:take])
        base_ok += all(any(j in base for j in g) for g in gs)

        rated = []
        for j in pool:
            p = judge_relevance(case["q"], bodies[j])
            rated.append((p if p is not None else 0.0, j))
        rated.sort(key=lambda x: -x[0])
        r12 = {j for _, j in rated[:take]}
        rr_ok += all(any(j in r12 for j in g) for g in gs)
        n += 1
        print(f"  {'✅' if all(any(j in r12 for j in g) for g in gs) else '◐' if any(any(j in r12 for j in g) for g in gs) else '❌'}"
              f"　池 {len(pool)} 个候选　{case['q'][:34]}", flush=True)
    print(f"\n—— 多跳全中率（n={n}）——")
    print(f"  不精排（向量序取前 {take}）：{base_ok}/{n} = {100*base_ok/n:.0f}%")
    print(f"  **本地判断器精排**：{rr_ok}/{n} = {100*rr_ok/n:.0f}%")
    print(f"  对照：4b 生成式 listwise 精排是 42%、不精排 50%（12 题那批）")


def psql_rows(sql):
    BS = chr(92)
    _ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}

    def un(s):
        o, i = [], 0
        while i < len(s):
            if s[i] == BS and i + 1 < len(s):
                o.append(_ESC.get(s[i + 1], BS + s[i + 1])); i += 2
            else:
                o.append(s[i]); i += 1
        return "".join(o)

    f = os.path.join(HERE, "_lj.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(r"D:\vs\rag-kb\data\pgapp.txt", encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([r"D:\vs\rag-kb\pgsql\bin\psql.exe", "-h", "127.0.0.1", "-U", "ragkb",
                          "-d", "ragkb", "-f", f], capture_output=True, env=env
                         ).stdout.decode("utf-8", "replace")
    return [[un(x) for x in ln.rstrip("\r").split("\t")] for ln in raw.split("\n") if ln.strip()]


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    mode = sys.argv[1] if len(sys.argv) > 1 else "sanity"
    sanity() if mode == "sanity" else rerank()
