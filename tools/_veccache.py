"""
向量缓存的**唯一**读入口 —— 读之前先验 `ids`。

为什么要有这个文件（2026-09-20）：
    复刻类尺子的写法一直是 `json.load("_corpus_vecs.json")["vecs"]`，**默认第 k 条就是
    现在库里第 k 块**。生产方（rerank-probe / multihop-probe）写的时候存了 `ids` 也验了
    `ids`，但**消费方把 `ids` 整个无视了**。

    今天实测：缓存是 00:29 写的（ids 3148~3808），之后语料被重建（现在 ids 5792~6452），
    结果 **661 条里 438 条错位**（k≥223 全错，cos 0.35~0.57）。而所有读它的尺子**不会报错**，
    只会让指标一起变低 —— 看起来像"检索就是不行"，实际是**索引错了**。
    这正是"全场 0 / 全场满分先当仪器故障"那条教训的第三次现身。

判据是机械的：`d["ids"] == ids`（两边都是按 `ORDER BY c.id` 取的字符串列表）。
对不上就重建，并把差异**打出来** —— 让它以后是"响一声"而不是"静默拉低"。
"""
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, ids, texts=None, embed=None, rebuild=True):
    """读缓存。`ids` 是按 `ORDER BY c.id` 取出的 id 列表。

    给了 `texts` + `embed` 就能自愈（对不上就重建）；只验证的话省略它们 ——
    对不上就**直接拒绝跑**，别让错位的索引静默拉低指标。
    """
    p = os.path.join(HERE, name)
    d = None
    if os.path.exists(p):
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception as e:                                   # 半截文件
            print(f"！{name} 读不动（{e}）")
        if d and d.get("ids") == ids:
            return d["vecs"]
        if d:
            cids = d.get("ids") or []
            print(f"！{name} 与当前语料**不符** —— 缓存 {len(cids)} 条（首 id "
                  f"{cids[0] if cids else '?'}）／现在 {len(ids)} 条（首 id {ids[0]}）"
                  f"。位置映射不可信")
    if not rebuild or texts is None or embed is None:
        raise SystemExit(f"{name} 已过期或不存在 —— 先跑："
                         f"python tools/vec-index-freshness-probe.py --rebuild")
    vecs = embed(texts)
    io.open(p, "w", encoding="utf-8").write(json.dumps({"ids": ids, "vecs": vecs}))
    print(f"  已重建 {name}（{len(ids)} 条）")
    return vecs
