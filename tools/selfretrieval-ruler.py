# -*- coding: utf-8 -*-
"""
下游尺子：**自检索**（self-retrieval）。

要回答的问题：分块边界切得对不对，对检索有没有**可测的**代价？

做法：从每个块自己身上取一句**正文**当查询，去全库检索，看这个块自己能不能被召回进 top-k。
道理链是：块从代码中段切出来 → 它的向量被代码主导 → 问它里面那段正文，召回的是**别的**块
→ 那段正文事实上**不可达**。这不是"切得好看不好看"，是"这段内容还能不能被找到"。

为什么这个量**没饱和**：它不依赖那 15 道题（已满分），而是对**每一个块**都问一次。

对照设计（关键）：**只动边界，不动粒度** ——
  A 现状：线上那 97 个块的边界
  B 修正：把落在"低中文占比连续段"里的边界，挪到该段落的最近外侧（其余一律不动）
两者的块数与长度分布基本不变，所以差异只能来自**边界位置**。

用法：python tools/selfretrieval-ruler.py [topk，默认 10]
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
EMBED = "bge-m3"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
CJK = re.compile(r"[\u4e00-\u9fff]")
CODE_LINE = re.compile(r"^\s*(public|private|import|class|def|SELECT|while|for|if|return|long|int)\b")


def psql_rows(sql):
    f = os.path.join(HERE, "_sr.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        for a, b in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(a, b)
        out.append(line)
    return out


def ratio(s):
    return len(CJK.findall(s)) / max(len(s), 1)


def embed(texts):
    out = []
    for i in range(0, len(texts), 8):
        body = {"model": EMBED, "input": texts[i:i + 8]}
        req = urllib.request.Request(OLLAMA + "/api/embed", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            out += json.load(r)["embeddings"]
    return out


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** .5
    nb = sum(x * x for x in b) ** .5
    return s / (na * nb + 1e-9)


def selfretrieval(chunks, topk=10):
    """对每个块取一句正文当查询，看它自己能不能被召回。返回 (命中率@1/@k, 平均名次)"""
    vecs = embed(chunks)
    hit1 = hitk = 0
    ranks = []
    for i, c in enumerate(chunks):
        q = pick_query(c)
        if q is None:
            continue
        qv = embed([q])[0]
        sims = sorted(((cos(qv, v), j) for j, v in enumerate(vecs)), reverse=True)
        order = [j for _, j in sims[:topk]]
        r = order.index(i) + 1 if i in order else 0
        ranks.append(r if r else topk + 1)
        hit1 += (r == 1)
        hitk += (r > 0)
    n = len(ranks)
    return {"n": n, "hit1": hit1 / max(n, 1), "hitk": hitk / max(n, 1),
            "mean_rank": sum(ranks) / max(n, 1)}


def pick_query(chunk):
    """从块里取一句**正文**（跳过代码味片段、跳过太短的），取中间那几段之一"""
    sents = [s.strip() for s in re.split(r"[。！？\n]", chunk) if len(s.strip()) >= 14]
    prose = [s for s in sents if ratio(s) >= 0.45 and not CODE_LINE.match(s)]
    if not prose:
        return None
    return prose[len(prose) // 2][:120]


def rebuild(segs, bounds):
    """按给定边界把段拼成块（bounds 为 1-based 段号，表示下一块的起点）"""
    idx = [0] + [b - 1 for b in bounds] + [len(segs)]
    return ["".join(segs[idx[i]:idx[i + 1]]).strip() for i in range(len(idx) - 1)]


def fix_bounds(segs, bounds, thr=0.25, max_shift=3):
    """把落在「低中文占比连续段」里的边界挪到该段落的最近外侧。"""
    out, prev = [], 0
    for b in bounds:
        nb = None
        for d in range(0, max_shift + 1):
            for cand in (b - d, b + d):
                if cand <= prev or cand >= len(segs):
                    continue
                # 位置 cand 合法 ⇔ cand-1 与 cand 至少有一侧是正文
                if ratio(segs[cand - 1]) >= thr or ratio(segs[cand]) >= thr:
                    nb = cand
                    break
            if nb:
                break
        nb = nb or b
        out.append(nb)
        prev = nb
    return out


def seg_owner(bounds, n):
    """每个段属于哪个块（0-based 块号）"""
    own, k = [], 0
    for i in range(n):
        while k < len(bounds) and i + 1 >= bounds[k]:
            k += 1
        own.append(k)
    return own


# 用户是**提问**，不是引原文。逐字查询太容易（原句就在块里，词面重合强，切哪儿都匹配得上）
# —— 第一版就是这么饱和掉的。这里改成"给每段生成一个它会回答的问题"，
# 用线上语境行那条提示词的问法（「这段能回答什么问题」）。
Q_PROMPT = """下面是一段资料。请写出**它能回答的一个问题** —— 用提问者的说法，不要用资料里的措辞照抄。

只输出 JSON：{"q":"问题"}"""

Q_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def gen_questions(segs, qi, model=None, cache=None):
    model = model or os.environ.get("KB_MODEL", "qwen3:4b")
    if cache and os.path.exists(cache):
        d = json.load(io.open(cache, encoding="utf-8"))
        if d.get("qi") == qi:
            return d["qs"]
    qs = []
    for i in qi:
        body = {"model": model, "stream": False, "think": False, "format": Q_SCHEMA,
                "options": {"temperature": 0.2, "num_ctx": 4096},
                "messages": [{"role": "system", "content": Q_PROMPT},
                             {"role": "user", "content": segs[i].strip()[:1500]}]}
        req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = json.load(r).get("message", {}).get("content", "")
        try:
            qs.append(str(json.loads(raw).get("q", "")).strip())
        except Exception:
            qs.append("")
    if cache:
        io.open(cache, "w", encoding="utf-8").write(
            json.dumps({"qi": qi, "qs": qs}, ensure_ascii=False))
    return qs


def run_fixed_queries(chunks, segs, bounds, qi, topk, questions=None):
    """**固定查询集**：查询来自源文本的段（与怎么分块无关），
    只问"装着这一段文本的那个块，能不能被召回"。queries 给定时用问题，否则用逐字原文。"""
    vecs = embed(chunks)
    own = seg_owner(bounds, len(segs))
    hit1 = hitk = 0
    ranks = []
    for k, i in enumerate(qi):
        q = (questions[k] if questions else segs[i].strip()[:120])
        if not q:
            continue
        qv = embed([q])[0]
        sims = sorted(((cos(qv, v), j) for j, v in enumerate(vecs)), reverse=True)
        order = [j for _, j in sims[:topk]]
        want = own[i]
        r = order.index(want) + 1 if want in order else 0
        ranks.append(r if r else topk + 1)
        hit1 += (r == 1)
        hitk += (r > 0)
    n = len(ranks)
    return {"n": n, "hit1": hit1 / max(n, 1), "hitk": hitk / max(n, 1),
            "mean_rank": sum(ranks) / max(n, 1)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    topk = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    segs = json.load(io.open(os.path.join(HERE, "..", "data", "_bintree-segs.json"),
                             encoding="utf-8"))["segs"]
    A = json.load(io.open(os.path.join(HERE, "_bounds_chunking.json"), encoding="utf-8"))["bounds"]
    B = fix_bounds(segs, A)
    shifted = [(x, y) for x, y in zip(A, B) if x != y]
    print(f"边界 {len(A)} 个，其中被挪动 {len(shifted)} 个：{shifted[:12]}{'…' if len(shifted) > 12 else ''}\n")

    ca, cb = rebuild(segs, A), rebuild(segs, B)
    print(f"A 现状块：{len(ca)} 个，长度中位 {sorted(len(c) for c in ca)[len(ca)//2]}")
    print(f"B 修正块：{len(cb)} 个，长度中位 {sorted(len(c) for c in cb)[len(cb)//2]}\n")

    # **固定查询集**：正文段里每 5 个取 1（若一段太短或太代码就不取）——两边问的是同一批问题
    qi = [i for i in range(len(segs))
          if ratio(segs[i]) >= 0.45 and len(segs[i].strip()) >= 30][::5]
    use_q = "--questions" in sys.argv
    qs = None
    if use_q:
        qs = gen_questions(segs, qi, cache=os.path.join(HERE, "_selfret_questions.json"))
        print(f"问题 {len(qs)} 条（用语境行问法生成，两边问同一批）")
        print(f"  例：{qs[0][:60] if qs else ''}")
    print(f"固定查询 {len(qi)} 条（取自源文本的段，与分块方式无关）"
          f"　模式：{'提问' if use_q else '逐字原文'}\n")

    ra = run_fixed_queries(ca, segs, A, qi, topk, qs)
    rb = run_fixed_queries(cb, segs, B, qi, topk, qs)
    print(f"{'':<10}{'查询数':>7}{'装着它的块排第1':>15}{f'进top{topk}':>11}{'平均名次':>10}")
    for tag, r in (("A 现状", ra), ("B 修正", rb)):
        print(f"{tag:<10}{r['n']:>7}{r['hit1']*100:>14.0f}%{r['hitk']*100:>10.0f}%"
              f"{r['mean_rank']:>10.1f}")
    print(f"\n差：top1 {100*(rb['hit1']-ra['hit1']):+.0f} 点　top{topk} {100*(rb['hitk']-ra['hitk']):+.0f} 点　"
          f"平均名次 {rb['mean_rank']-ra['mean_rank']:+.1f}")
    print("\n另：A 能取出正文句的块 vs B（反映有多少块被代码主导）")
    print(f"    A {sum(1 for c in ca if pick_query(c))} / {len(ca)}　"
          f"B {sum(1 for c in cb if pick_query(c))} / {len(cb)}")


if __name__ == "__main__":
    main()
