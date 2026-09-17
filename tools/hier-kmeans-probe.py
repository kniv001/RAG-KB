# -*- coding: utf-8 -*-
"""
结构化切分·按「局部切分 + k-means 归并」这条路走一遍。

为什么不让模型管章层：实测它做全局归并时要么不切（整篇一章）、要么凑数
（章的段数几乎全是 5，尽管提示词明说「不要按段数平均分」）。而「把相近的东西聚一起」
本来就不需要语言模型 —— 系统里的主题树就是 k-means 建的。

所以这条路的形状是：
  ① 叶子层：模型逐块切分（局部判断，已验证可靠：~320ms/块、零摆烂、逐字重构）
  ② 章层：k-means over 段向量（几何，无模型）
  ③ 命名：每簇一次局部 LLM 调用（与主题树同一套做法）

顺带比一个东西：**同样用 k-means，聚「段」和聚「块」哪个更内聚** ——
这决定了层级的下层该用段还是直接用块。

用法：python tools/hier-kmeans-probe.py [文档id] [簇数]
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import numpy as np

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT, EMBED = "qwen3:4b", "bge-m3"
BS = chr(92)

LEAF_PROMPT = """下面这段资料已按句子编号（1 到 N）。请按**语义**把它切成若干段，每段讲一件事。

只输出每段的**结束句编号**，JSON：{"ends":[3,7,12]}

要求：
- 编号从 1 开始、严格递增，**最后一项必须等于 N**
- 每段 2~8 句；不要把整段当成一段，也不要逐句切
- 不要改写、不要解释"""

# 示例值不能写成「概括」这类提示词里出现过的词 —— 模型会把示例字面抄回来
# （上一轮 11 个簇里 9 个的名字就叫「概括」）。示例必须是本语料里真会出现的词。
NAME_PROMPT = """下面是同一主题下的若干片段。用一句话（不超过 30 字）概括它们讲的是什么。

只输出 JSON：{"label":"..."}"""
LEAF_SCHEMA = {"type": "object",
               "properties": {"ends": {"type": "array", "items": {"type": "integer"}}},
               "required": ["ends"]}
NAME_SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}},
               "required": ["label"]}

# 第三步的输入是**标签序列**而不是原始段 —— k-means 已经替模型做完了「哪些片段相似」，
# 它只剩「在哪里换章」这一个判断，而那是它做得动的规模。
# 单值决策的提示词：只问「最大的转换点在哪」，输出一个下标。
# 列表式输出里「只切一刀」是合法解（逃生口），而单值决策一旦离谱就能机械识别并修。
SPLIT_PROMPT = """下面是一篇文档的片段序列（按文中顺序）。请找出其中**最大的一次主题转换**发生在哪里 ——
也就是「从这里开始，讲的东西明显换了一件」的那一处。

只输出 JSON：{"at":7}

要求：
- at 是「转换前最后一项」的项号，必须落在 {LO} 到 {HI} 之间（含）
- 必须给出一个真正的转换点：**不要贴边**，也不要挑无关紧要的换标签
- 不要解释"""

SPLIT_SCHEMA = {"type": "object", "properties": {"at": {"type": "integer"}}, "required": ["at"]}

CHAPTER_PROMPT = """下面是一篇文档的片段序列，按文中出现顺序给出。每项是「主题标签 + 该标签连续出现的段数」。

请按**主题转换**把它们切成若干章：同一章里讲同一件事。

只输出每章的**结束项号**，JSON：{"ends":[3,7,12]}

要求：
- 项号从 1 开始、严格递增，**最后一项必须等于 {S}**
- 每章 2~8 项（项 = 一段连续同标签的片段，不是原文段落）
- 边界落在主题转换处；**不要按项数平均分**
- 不要解释"""
# 示例值同样不能写成像样的词 —— format 约束下会被字面抄回来（今天已踩两次）
CJK = re.compile(r"[\u4e00-\u9fff]")


def psql_rows(sql):
    f = os.path.join(HERE, "_hk.sql")
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
        for esc, real in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(esc, real)
        out.append(line)
    return out


def psql(sql):
    f = os.path.join(HERE, "_hk2.sql")
    io.open(f, "w", encoding="utf-8").write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    return subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                           "-t", "-A", "-f", f], capture_output=True, env=env).stdout.decode("utf-8", "replace")


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def sentences(text):
    out, start, line_start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c in "。！？!?；;":
            out.append(text[start:i + 1]); start = i + 1; line_start = i + 1
        elif c == "\n":
            j = text.find("\n", i + 1)
            j = len(text) if j < 0 else j
            if not CJK.search(text[line_start:i]) or not CJK.search(text[i + 1:j]):
                out.append(text[start:i + 1]); start = i + 1
            line_start = i + 1
        i += 1
    if start < len(text):
        out.append(text[start:])
    return [s for s in out if s.strip()] or [text]


def ask(system, user, schema):
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": schema,
                           "options": {"temperature": 0.1, "num_ctx": 16384},
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]})
    txt = r.get("message", {}).get("content", "")
    try:
        return json.loads(txt)
    except Exception:
        return {}


def embed_all(texts, tag=""):
    out = []
    for i in range(0, len(texts), 16):
        out.extend(post("/api/embed", {"model": EMBED, "input": texts[i:i + 16]})["embeddings"])
        if tag and i % 1600 == 0:
            print(f"    嵌入 {tag} {i}/{len(texts)}", flush=True)
    a = np.array(out, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def kmeans(X, k, seed=42, iters=40):
    rng = np.random.default_rng(seed)
    # k-means++ 初始化（与主题树同一套思路：离已有质心越远越可能被选中）
    C = [X[rng.integers(len(X))]]
    for _ in range(k - 1):
        d = 1 - np.max(np.stack([X @ c for c in C]), axis=0)
        p = np.maximum(d, 0) ** 2
        p = p / p.sum() if p.sum() > 0 else np.ones(len(X)) / len(X)
        C.append(X[rng.choice(len(X), p=p)])
    C = np.stack(C)
    for _ in range(iters):
        a = np.argmax(X @ C.T, axis=1)
        newC = np.stack([X[a == j].mean(axis=0) if (a == j).any() else C[j] for j in range(k)])
        newC /= np.maximum(np.linalg.norm(newC, axis=1, keepdims=True), 1e-9)
        if np.allclose(newC, C, atol=1e-5):
            C = newC
            break
        C = newC
    return np.argmax(X @ C.T, axis=1), C


def tightness(X, assign, C):
    """内聚度：到本簇质心的平均余弦 / 到全局质心的平均余弦（越小越内聚）"""
    own = np.mean([X[i] @ C[assign[i]] for i in range(len(X))])
    g = X.mean(axis=0); g /= max(np.linalg.norm(g), 1e-9)
    glob = np.mean(X @ g)
    return own, glob, own / max(glob, 1e-9)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    doc = sys.argv[1] if len(sys.argv) > 1 else None
    if not doc:
        doc = psql("SELECT doc_id FROM chunks GROUP BY doc_id ORDER BY count(*) DESC LIMIT 1;").strip().splitlines()[0].strip()
    name = psql(f"SELECT name FROM documents WHERE id='{doc}';").strip().splitlines()[0]
    chunks = psql_rows(f"SELECT content FROM chunks WHERE doc_id='{doc}' ORDER BY seq")
    print(f"文档：{name[:44]}　{len(chunks)} 块\n")

    # ① 叶子层
    print("—— ① 叶子层：模型逐块切分 ——")
    segs, t0, bail = [], time.time(), 0
    for i, c in enumerate(chunks, 1):
        ss = sentences(c)
        if len(ss) < 3:
            segs.append(c)
            continue
        numbered = "\n".join(f"{j+1}. {s.strip()}" for j, s in enumerate(ss))
        d = ask(LEAF_PROMPT.replace("N", str(len(ss))), numbered, LEAF_SCHEMA)
        e = sorted({x for x in d.get("ends", []) if isinstance(x, int) and 1 <= x <= len(ss)}) or [len(ss)]
        if e[-1] != len(ss):
            e.append(len(ss))
        if len(e) == 1:
            bail += 1
        st = 0
        for x in e:
            piece = "".join(ss[st:x]).strip()
            if piece:
                segs.append(piece)
            st = x
    print(f"  {len(chunks)} 块 → {len(segs)} 段（{len(segs)/len(chunks):.1f} 段/块，"
          f"整块未切的 {bail} 块），耗时 {time.time()-t0:.0f}s")
    print(f"  平均 token/段 ≈ {np.mean([len(s) for s in segs])/1.5:.0f}")

    # ② 章层：k-means
    k = int(sys.argv[2]) if len(sys.argv) > 2 else max(4, min(12, int((len(segs) / 2) ** 0.5)))
    print(f"\n—— ② 章层：k-means（k={k}）——")
    Xs = embed_all([s for s in segs], "段")
    Xc = embed_all(chunks, "块")
    asg_s, Cs = kmeans(Xs, k)
    asg_c, Cc = kmeans(Xc, k)
    for tag, X, asg, C in (("聚「段」", Xs, asg_s, Cs), ("聚「块」", Xc, asg_c, Cc)):
        own, glob, ratio = tightness(X, asg, C)
        sizes = np.bincount(asg, minlength=k)
        print(f"  {tag}：{len(X)} 个单元　簇大小 {sorted(sizes.tolist(), reverse=True)}")
        print(f"        内聚度：到本簇质心 {own:.3f} / 到全局质心 {glob:.3f} = **{ratio:.3f}**（越小越内聚）")

    # ③ 连续性：k-means 给的是「主题簇」，不是「结构章」—— 后者必须是连续跨度
    print("\n—— ③ 连续性：主题簇能不能直接当章 ——")
    scat = []
    for j in range(k):
        idx = np.where(asg_s == j)[0]
        span = int(idx.max() - idx.min() + 1)
        scat.append(span / len(idx))
    print(f"  散布比（簇跨度 / 成员数）：中位 {np.median(scat):.2f}，最大 {max(scat):.2f}")
    print(f"  （=1 表示成员连续成段可作章；≫1 表示散布全文，只能当主题不能用章）")

    # ④ 平滑成连续章：沿序列把主题标签切成连续段（短跳变的先忽略，抗噪）
    labels = [f"t{int(a)}" for a in asg_s]
    runs, cur, cnt = [], labels[0], 1
    for lab in labels[1:]:
        if lab == cur:
            cnt += 1
        else:
            runs.append([cur, cnt])
            cur, cnt = lab, 1
    runs.append([cur, cnt])
    merged = []
    for r in runs:                      # 太短的跳变并回上一章（噪声）
        if merged and r[1] < 3:
            merged[-1][1] += r[1]
        else:
            merged.append(r)
    print(f"\n—— ④ 平滑成连续章 ——")
    print(f"  {len(labels)} 段 → {len(merged)} 章，章段数 {[m[1] for m in merged]}")

    # ⑤ 命名（每簇一次局部调用）
    print("\n—— ⑤ 每簇命名 ——")
    names = {}
    for j in range(k):
        idx = np.where(asg_s == j)[0]
        sample = "\n".join(f"- {segs[i][:70]}" for i in idx[:5])
        d = ask(NAME_PROMPT, sample, NAME_SCHEMA)
        names[j] = (d.get("label") or "").strip()[:30] or f"簇{j}"
        print(f"  簇{j}（{len(idx)} 段）　{names[j]}")

    # ⑥ 三步版的第三步：在**标签序列**上让模型定章（而不是在原始段上）
    #    k-means 把 245 段压成 11 个标签，模型要判的规模就从 245 降到几十个「连续同标签段」。
    runs2 = []
    for lab, size in runs:
        if runs2 and runs2[-1][0] == lab:
            runs2[-1][1] += size
        else:
            runs2.append([lab, size])
    listing = "\n".join(
        f"{i+1}. [{names[int(l[1:])]}] × {n} 段" for i, (l, n) in enumerate(runs2))
    print(f"\n—— ⑥ 在标签序列上定章（模型，输入 {len(runs2)} 项）——")
    prompt = CHAPTER_PROMPT.replace("{S}", str(len(runs2)))
    d = ask(prompt, listing, LEAF_SCHEMA)
    e = sorted({x for x in d.get("ends", []) if isinstance(x, int) and 1 <= x <= len(runs2)}) or [len(runs2)]
    if e[-1] != len(runs2):
        e.append(len(runs2))
    sizes2, prev = [], 0
    for x in e:
        sizes2.append(x - prev)
        prev = x
    n_seg = sum(r[1] for r in runs2)
    ok_nondeg = len(e) >= 3 and max(sizes2) <= len(runs2) * 0.6
    print(f"  {len(runs2)} 项 → {len(e)} 章，章项数 {sizes2}")
    print(f"  对应段数 {[sum(runs2[i][1] for i in range(s, t)) for s, t in
                        zip([0]+e[:-1], e)]}")
    print(f"  非退化：{'✅' if ok_nondeg else '❌'}")
    # 每章的主标签，供人读
    pos = 0
    for rank, sz in enumerate(sizes2, 1):
        labs = [runs2[i][0] for i in range(pos, pos + sz)]
        pos += sz
        main = max(set(labs), key=labs.count)
        print(f"    第{rank}章　{sz} 项 / {sum(runs2[i][1] for i in range(pos-sz, pos))} 段"
              f"　主标签「{names[int(main[1:])]}」")

    # ⑦ 递归二分：每次只问「最大转换点在哪」——单值决策，落在模型的可靠区间内
    print("\n—— ⑦ 递归二分找章（每次一个单值决策）——")
    line = "\n".join(f"{i+1}. [{names[int(l[1:])]}] × {n} 段"
                     for i, (l, n) in enumerate(runs2))
    lines = line.split("\n")
    calls = [0]

    def bisect(lo, hi, out):
        """在 runs2 的 [lo,hi) 区间上递归切分，每章不超过 MAXRUN 项"""
        if hi - lo <= 14:
            out.append((lo, hi))
            return
        m = hi - lo
        a, b = lo + max(1, int(m * 0.15)), hi - max(1, int(m * 0.15))
        calls[0] += 1
        d = ask(SPLIT_PROMPT.replace("{LO}", str(a + 1)).replace("{HI}", str(b)),
                "\n".join(lines[lo:hi]), SPLIT_SCHEMA)
        at = d.get("at")
        if not isinstance(at, int) or not (a + 1 <= at <= b):
            at = lo + m // 2          # 机械修复：离谱就取中点，绝不因此摆烂
        at -= 1                        # 转成 0-based 的「最后一项下标」
        if at <= lo or at >= hi - 1:
            at = lo + m // 2
        bisect(lo, at + 1, out)
        bisect(at + 1, hi, out)

    chaps = []
    bisect(0, len(runs2), chaps)
    print(f"  调用 {calls[0]} 次 → {len(chaps)} 章，章项数 {[b - a for a, b in chaps]}")
    print(f"  对应段数 {[sum(runs2[i][1] for i in range(a, b)) for a, b in chaps]}")
    for rank, (a, b) in enumerate(chaps, 1):
        labs = [runs2[i][0] for i in range(a, b)]
        main = max(set(labs), key=labs.count)
        seg0 = sum(runs2[i][1] for i in range(0, a))
        print(f"    第{rank}章　{b-a} 项 / {sum(runs2[i][1] for i in range(a, b))} 段"
              f"（起始段 {seg0+1}）　主标签「{names[int(main[1:])]}」")

    # 边界两侧各看一句 —— 形状是算法给的，但边界该落在主题转换上，这一条只能读
    print("\n—— 边界两侧（判断边界是不是真的落在转换处）——")
    starts = [sum(runs2[i][1] for i in range(0, a)) for a, _ in chaps] + [len(segs)]
    for i in range(1, len(chaps)):
        j = starts[i]
        before = segs[j - 1][-46:].replace("\n", " ") if j > 0 else ""
        after = segs[j][:46].replace("\n", " ") if j < len(segs) else ""
        print(f"  {i}|{i+1}  …{before}")
        print(f"        {after}…")


if __name__ == "__main__":
    main()
