"""
向量索引新鲜度体检：**`_corpus_vecs.json` 里的第 k 条，是不是现在库里第 k 块的向量**。

为什么需要这台仪器（2026-09-20）：
    复刻类尺子（logprob-judge / topk-ab / mmr-ab / threshold-ab / selfretrieval-ruler）
    全都这么写：`vecs = json.load("_corpus_vecs.json")["vecs"]`，然后拿 `vecs[j]` 当
    `rows[j]`（`ORDER BY c.id` 的第 j 行）的向量。**它们都无视了文件里的 `ids` 字段。**
    只要这个 json 是**旧语料**留下的，整把尺子就建在错位的索引上 —— 而它不会报错，
    只会让所有指标一起变低，看起来像"检索就是不行"。

    实测：`ids` 是 3148~3808，当前语料是 5792~6452（不同批次），
    且前 201 条恰好一致、之后全错位。

判据（机械、无阈值玄学）：
    标定 —— 同一段文本嵌两次必须 cos = 1.0000（bge-m3 是确定性的）。
    逐条比 cos(文件里第 k 条, 现算第 k 条的文本)，> 0.999 记"新鲜"，否则"陈"。
    另外单独比一次**去掉 ctx 前缀**的版本，用来区分两种病：
      · 与「带 ctx」吻合、与「不带 ctx」不吻合 ⇒ 文件是新的，只是建的时候带了 ctx
      · 两个都不吻合 ⇒ 文件是**旧语料**的（错位）

用法：python tools/vec-index-freshness-probe.py [步长，默认全量]
"""
import io
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
VECFILE = os.path.join(HERE, "_corpus_vecs.json")


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

    f = os.path.join(HERE, "_fresh.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(r"D:\vs\rag-kb\data\pgapp.txt", encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([r"D:\vs\rag-kb\pgsql\bin\psql.exe", "-h", "127.0.0.1", "-U", "ragkb",
                          "-d", "ragkb", "-f", f], capture_output=True, env=env
                         ).stdout.decode("utf-8", "replace")
    # 注意：psql COPY 在 Windows 上给的是 CRLF，末尾字段带 \r 会让文档名永远比不中
    return [[un(x) for x in ln.rstrip("\r").split("\t")] for ln in raw.split("\n") if ln.strip()]


def emb(texts, batch=8):
    out = []
    for i in range(0, len(texts), batch):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": texts[i:i + batch]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1800) as r:
            out.extend(json.load(r)["embeddings"])
    return out


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)


def rebuild(rows):
    """按**当前库**重建三个变体。文件名沿用 multihop-probe 的约定，别改。"""
    ids = [r[0] for r in rows]
    variants = {
        "_corpus_vecs.json": [(r[1] + "\n" + r[2]) if r[1] else r[2] for r in rows],  # ctx+body
        "_corpus_vecs-noctx.json": [r[2] for r in rows],                              # body
        "_corpus_vecs-ctxtonly.json": [r[1] for r in rows],                           # ctx
    }
    # **语料戳要写进每一个变体**（2026-09-22 修：重建时只有主缓存带戳，
    # 两个变体被写掉了戳 —— 而"每个数字带尺子名 + 语料戳"正是这个项目的纪律，
    # 缺了它事后回溯"这批向量是哪版语料的"就答不上来）。
    try:
        sys.path.insert(0, HERE)
        from ruler import corpus as _c
        stamp = _c.load().stamp
    except Exception:
        stamp = None
    for name, texts in variants.items():
        print(f"嵌入 {name}（{len(texts)} 条）…", flush=True)
        vecs = emb(texts)
        io.open(os.path.join(HERE, name), "w", encoding="utf-8").write(
            json.dumps({"stamp": stamp, "ids": ids, "vecs": vecs}))
        print(f"  写好 {name}（ids {ids[0]}~{ids[-1]}，戳 {stamp}）", flush=True)


def main():
    if sys.argv[1:2] == ["--rebuild"]:
        rows = psql_rows("SELECT c.id, coalesce(c.ctx,''), c.content, c.seq, d.name "
                         "FROM chunks c JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
        rebuild(rows)
        print("重建完成 —— 复刻类尺子可以重跑了")
        return
    step = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    rows = psql_rows("SELECT c.id, coalesce(c.ctx,''), c.content, c.seq, d.name "
                     "FROM chunks c JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    d = json.load(io.open(VECFILE, encoding="utf-8"))
    V = d["vecs"]
    print(f"文件 {len(V)} 条；库里 {len(rows)} 块；文件里的 ids 范围 "
          f"{min(d['ids'])}~{max(d['ids'])}；库里 id 范围 {rows[0][0]}~{rows[-1][0]}")

    # 标定：同文本两次
    probe = rows[0][2]
    c_same = cos(emb([probe])[0], emb([probe])[0])
    print(f"标定：同文本两次 cos = {c_same:.4f}（应 1.0000；若非 1，下面所有判读作废）")

    ks = list(range(0, min(len(V), len(rows)), step))
    with_ctx = [(rows[k][1] + "\n" + rows[k][2]) if rows[k][1] else rows[k][2] for k in ks]
    raw_only = [rows[k][2] for k in ks]
    Ew = emb(with_ctx)
    Er = emb(raw_only)

    fresh = stale = 0
    first_stale = None
    detail = []
    for n, k in enumerate(ks):
        cw, cr = cos(V[k], Ew[n]), cos(V[k], Er[n])
        ok = max(cw, cr) > 0.999
        if ok:
            fresh += 1
        else:
            stale += 1
            if first_stale is None:
                first_stale = k
        detail.append((k, cw, cr, ok))

    print(f"\n抽样 {len(ks)} 条（步长 {step}）：新鲜 {fresh}　陈 {stale}"
          f"　= {100*fresh/max(1,len(ks)):.0f}% 新鲜")
    if first_stale is not None:
        print(f"第一条陈的位置：k={first_stale}（chunk {rows[first_stale][0]}，"
              f"{rows[first_stale][4][:30]}）")
    print("\n前 12 条与断点附近：")
    show = detail[:12] + [x for x in detail if first_stale is not None
                          and first_stale - 3 <= x[0] <= first_stale + 6]
    for k, cw, cr, ok in show:
        print(f"  k={k:>4}　带ctx cos={cw:>7.4f}　裸文本 cos={cr:>7.4f}　"
              f"{'新鲜' if ok else '陈'}　chunk {rows[k][0]}　{rows[k][4][:26]}")

    print("\n判读：")
    print("  · 两个 cos 都低 ⇒ 文件是**旧语料**留下的，位置映射错位 —— 复刻尺子全部作废，须重建")
    print("  · 只有带 ctx 的高 ⇒ 文件新鲜，只是嵌入时带了 ctx（消费方按裸文本算也无妨）")
    print(f"  · 重建命令：重新嵌入全部 {len(rows)} 块（旧的 ids 字段别再信，写入时按 c.id 存）")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
