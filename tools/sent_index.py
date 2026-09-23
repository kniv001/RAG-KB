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
"""
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


def load_rows():
    return corpus.psql_rows(
        "SELECT chunk_id, seq, sent_hash, text FROM sentences ORDER BY chunk_id, seq")


def embed_and_cache(C, rows, force=False):
    """算句子向量并写**按内容锚**的缓存（stamp + 逐条 hash）。

    对不上就重算 —— 按位置对齐的缓存会随表重建**静默错位**
    （实测过：661 条里 438 条错、不报错、只让所有指标一起变低）。
    """
    hashes = [r[2] for r in rows]
    d = None
    if os.path.exists(VECFILE) and not force:
        try:
            d = json.load(io.open(VECFILE, encoding="utf-8"))
        except Exception as e:
            print(f"！{os.path.basename(VECFILE)} 读不动（{e}）")
    if d and d.get("stamp") == C.stamp and d.get("hashes") == hashes:
        return d["vecs"]
    why = "强制重算" if force else ("戳不同" if not d or d.get("stamp") != C.stamp
                                    else "**逐条 hash 对不上**（表被重建过）")
    print(f"算句子向量（{why}，{len(rows)} 句）…", flush=True)
    v = corpus.embed([r[3] for r in rows])
    io.open(VECFILE, "w", encoding="utf-8").write(
        json.dumps({"stamp": C.stamp, "hashes": hashes, "vecs": v}))
    return v


def fill_db(C, rows, V):
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
