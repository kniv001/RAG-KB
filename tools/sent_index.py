# -*- coding: utf-8 -*-
"""
**句子索引的公共件** —— 建表 / 算向量 / 灌库三件事各只有一份实现。

## 为什么要有这个模块（2026-09-23）

原先是三个脚本串起来跑：`build-sentences.py` → `sent-recall.py`（顺手把向量写进缓存）
→ `fill-sentence-vecs.py`。三个地方各自知道"用什么模型标识"，于是**灌库那一步写的
`bge-m3` 和 Java 查的 `local:bge-m3` 对不上** —— 症状是"分层注入 0 句、悄悄退回整块"，
靠一行覆盖率日志才抓到。

这正是本项目反复吃亏的形状：**同一件事散在多处，谁都不报错，只是悄悄不一致**。
所以：**模型标识只在这里读一次**（从 `chunks` 表读，跟库里的约定走），
建表与灌库都调这里的函数，不各写一份。

## 两个锚（2026-09-25 改的，别再用"全库语料戳"）

| 锚 | 值 | 回答什么问题 |
|---|---|---|
| `chunk_hash`（落库，每行） | `md5(这一块的正文)[:12]` | **这一行还有效吗** —— 比一下就行，不必重建 |
| 向量缓存的 key（`_sentence_vecs.json`） | `sha1(嵌进去的那串字)[:16]` | **这条向量还要不要重算** |

原先两者都锚在**全库语料戳**上（"块数 + 每块正文哈希"的再哈希）—— 而那个戳
**加一篇或删一篇文档就变**：晋升 40 篇新闻之后，904 块里只有 243 块是新的，
整个索引却显示"陈旧"，一重建就是 10052 句向量全算（几分钟显存）。
按上面的两个锚之后，同一次重建只算**真正新出现的句子**（这次是 0 句）。

⚠️ 缓存**按老格式**（`{stamp, hashes, vecs}`，位置对齐）时会被自动认出来并转成新格式 ——
但**只在逐条 hash 与传进来的正文对得上时**才转（对不上说明那份缓存对应的是**别的句子**，
按位置用会静默错位，本项目在向量缓存上吃过一次：661 条里 438 条错）。
"""
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ruler import corpus                                   # noqa: E402

VECFILE = os.path.join(HERE, "_sentence_vecs.json")


def model_id():
    """库里的向量模型标识（形如 `local:bge-m3`）。

    **从 chunks 表读，不硬写** —— Python 侧的常量是 `bge-m3`，而落库的约定带提供方前缀。
    写错不会报错，只会让按模型过滤的查询一条都命中不了。
    """
    rows = corpus.psql_rows("SELECT DISTINCT embed_model FROM chunks")
    if len(rows) != 1:
        raise SystemExit(f"chunks 表里的 embed_model 不唯一：{rows} —— 先弄清该用哪个")
    return rows[0][0]


def chunk_anchor(content):
    """块内容锚 —— 必须与 SQL 侧的 `left(md5(content),12)` **逐字一致**（freshness 靠它比）。"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def _vec_key(text):
    """向量缓存的 key：**嵌进去的那串字**的哈希（不是去空白后的 —— 嵌入用的是原文）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_rows():
    return corpus.psql_rows(
        "SELECT chunk_id, seq, sent_hash, text FROM sentences ORDER BY chunk_id, seq")


def freshness(c=None):
    """句子索引与语料的对齐情况 —— **一句 SQL 就能回答的事实**（不必重建）。

    返回 `{rows, chunks, stale, missing}`：行数 / 块数 / **锚对不上的行数** / **没有句子的块数**。
    `stale` 含 `chunk_hash IS NULL` 的旧行（那是"还没按新口径重算过"）。
    """
    c = c or corpus.load()
    r = corpus.psql_rows(
        "SELECT (SELECT count(*) FROM sentences),"
        " (SELECT count(*) FROM chunks),"
        " (SELECT count(*) FROM sentences s JOIN chunks c ON c.id = s.chunk_id"
        "    WHERE s.chunk_hash IS DISTINCT FROM left(md5(c.content), 12)),"
        " (SELECT count(*) FROM chunks c"
        "    WHERE NOT EXISTS (SELECT 1 FROM sentences s WHERE s.chunk_id = c.id))")[0]
    return {"rows": int(r[0]), "chunks": int(r[1]), "stale": int(r[2]), "missing": int(r[3])}


def freshness_line(c=None):
    f = freshness(c)
    ok = f["stale"] == 0 and f["missing"] == 0
    return (f"句子索引 {f['rows']} 行 / {f['chunks']} 块　"
            + ("✓ 与语料**逐块对齐**" if ok else
               f"✗ **陈旧 {f['stale']} 行、缺句子 {f['missing']} 块**"
               f"（重建：python tools/build-sentences.py）"))


def _load_cache():
    d = None
    if os.path.exists(VECFILE):
        try:
            d = json.load(io.open(VECFILE, encoding="utf-8"))
        except Exception as e:
            print(f"！{os.path.basename(VECFILE)} 读不动（{e}）")
    return d or {}


def embed_and_cache(texts, force=False):
    """算向量，写**按内容锚**的缓存 `{model, keyed: {key → 向量}}`，返回**与传入正文对齐**的向量表。

    只算 key 不在缓存里的那些 —— 这是"加文档不再全量重算"的落点。
    """
    model = model_id()
    d = _load_cache()
    keyed = d.get("keyed") if isinstance(d.get("keyed"), dict) else None

    if keyed is None and d.get("vecs") and d.get("hashes"):
        # 老格式：{stamp, hashes, vecs}（**位置对齐**）。不按位置搬，按 hash 查 ——
        # 位置对齐正是这个项目吃过亏的东西（661 条里 438 条错位、不报错）；
        # 而 hash 是从正文算的，**对得上就一定是同一句**，对不上就不搬。
        by_hash = {}
        for h, v in zip(d["hashes"], d["vecs"]):
            by_hash.setdefault(h, v)
        n0 = len(by_hash)
        keyed = {}
        hit = 0
        for t in dict.fromkeys(texts):
            v = by_hash.get(hashlib.sha1(t.strip().encode("utf-8")).hexdigest()[:12])
            if v is not None:
                keyed[_vec_key(t)] = v
                hit += 1
        print(f"  缓存是老格式（位置对齐，{n0} 条）—— 按内容 hash 搬过来 {hit} 条"
              f"，其余重算（位置一个都不信）")
    if keyed is None:
        keyed = {}

    if d.get("model") not in (None, model) and keyed:
        print(f"！缓存是 {d.get('model')} 算的，而库里的模型是 {model} —— 整份重算")
        keyed = {}

    uniq = list(dict.fromkeys(texts))                       # 去重（同一句可能出现在多处）
    missing = [t for t in uniq if _vec_key(t) not in keyed]
    if not missing and not force:
        print(f"句子向量：{len(texts)} 句（去重 {len(uniq)}）**全部命中缓存**")
    else:
        print(f"句子向量：{len(texts)} 句（去重 {len(uniq)}）—— 缓存命中 "
              f"{len(uniq) - len(missing)}，**新算 {len(missing)}**…", flush=True)
        for t, v in zip(missing, corpus.embed(missing)):
            keyed[_vec_key(t)] = v

    io.open(VECFILE, "w", encoding="utf-8").write(
        json.dumps({"model": model, "keyed": keyed}))
    return [keyed[_vec_key(t)] for t in texts]


def fill_db(rows, V):
    """把向量灌进 `sentences.embedding`（分层注入用）。"""
    model = model_id()
    if len(V) != len(rows):
        raise SystemExit(f"条数不符：向量 {len(V)} / 表 {len(rows)}")

    def lit(v):
        return "[" + ",".join(f"{x:.7g}" for x in v) + "]"

    buf = io.StringIO()
    buf.write("BEGIN;\n")
    buf.write("CREATE TEMP TABLE _sv (chunk_id bigint, seq int, emb text) ON COMMIT DROP;\n")
    buf.write("COPY _sv (chunk_id, seq, emb) FROM STDIN;\n")
    for r, v in zip(rows, V):
        buf.write(f"{r[0]}\t{r[1]}\t{lit(v)}\n")
    buf.write("\\.\n")
    buf.write("UPDATE sentences s SET embedding = v.emb::vector, embed_model = '%s'\n"
              "  FROM _sv v WHERE s.chunk_id = v.chunk_id AND s.seq = v.seq;\n" % model)
    buf.write("COMMIT;\n")
    corpus.psql_script(buf.getvalue(), tag="sentvec")
    back = corpus.psql_rows(
        "SELECT count(*), count(embedding), count(distinct embed_model) FROM sentences")
    print(f"已灌向量：{back[0][0]} 行，其中**有向量的 {back[0][1]}**，模型 {model}")
    if back[0][1] != back[0][0]:
        raise SystemExit("✗ 有行没灌上 —— 别放过：空向量会让那句永远排不上")
