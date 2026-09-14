"""检索：问题 -> 相关分块。

三种模式：
  vector   —— 纯 pgvector 余弦检索（走 HNSW 索引）
  keyword  —— 关键词召回（ASCII 词 + 中文二元组，ILIKE 计数打分）
  hybrid   —— 两路召回后用 RRF（Reciprocal Rank Fusion）融合，默认

魔改点：
  · 加查询改写 / HyDE：在 search() 开头改写 question
  · 加 rerank：把候选集放大（RERANK_POOL）后精排再截断
  · 换融合算法：改 _rrf
"""

from __future__ import annotations

import re

from app import config, db, embed

MODE_DEFAULT = "hybrid"
RERANK_POOL = 30  # 每路召回的候选数，融合后再截断到 top_k

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")

_VECTOR_SQL = """
SELECT c.id, c.doc_id, c.seq, c.content, d.name AS doc_name,
       (c.embedding <=> %(q)s::vector) AS distance
FROM chunks c
JOIN documents d ON d.id = c.doc_id
WHERE c.embedding IS NOT NULL
  AND c.embed_model = %(m)s
  AND (%(doc)s::text IS NULL OR c.doc_id = %(doc)s::text)
ORDER BY c.embedding <=> %(q)s::vector
LIMIT %(n)s
"""

_KEYWORD_SQL = """
SELECT c.id, c.doc_id, c.seq, c.content, d.name AS doc_name,
       (SELECT count(*) FROM unnest(%(terms)s::text[]) t
          WHERE c.content ILIKE '%%' || t || '%%') AS hits
FROM chunks c
JOIN documents d ON d.id = c.doc_id
WHERE c.embed_model = %(m)s
  AND (%(doc)s::text IS NULL OR c.doc_id = %(doc)s::text)
ORDER BY hits DESC, c.id
LIMIT %(n)s
"""


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def terms(question: str) -> list[str]:
    """抽取关键词：英文数字词 + 中文二元组。"""
    words = _WORD.findall(question)
    chars = _CJK.findall(question)
    bigrams = ["".join(p) for p in zip(chars, chars[1:])]
    singles = chars if len(chars) == 1 else []
    return list(dict.fromkeys(words + bigrams + singles))[:24]


def _vector_hits(question: str, n: int, doc_id: str | None) -> list[dict]:
    vec = embed.embed_query(question)
    current = embed.current_model()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            _VECTOR_SQL,
            {"q": _literal(vec), "m": current, "doc": doc_id, "n": n},
        )
        return cur.fetchall()


def _keyword_hits(question: str, n: int, doc_id: str | None) -> list[dict]:
    ts = terms(question)
    if not ts:
        return []
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            _KEYWORD_SQL,
            {"terms": ts, "m": embed.current_model(), "doc": doc_id, "n": n},
        )
        return [r for r in cur.fetchall() if r["hits"] > 0]


def _rrf(*ranked_lists: list[dict], k: int = 60) -> dict:
    """Reciprocal Rank Fusion：只看排名不看原始分数，天然免疫两路分数量纲不同。"""
    scores: dict = {}
    for lst in ranked_lists:
        for rank, row in enumerate(lst, 1):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (k + rank)
    return scores


def search(
    question: str,
    top_k: int | None = None,
    max_distance: float | None = None,
    mode: str = MODE_DEFAULT,
    doc_id: str | None = None,
) -> list[dict]:
    """返回按相关性排序的分块。"""
    k = top_k or config.TOP_K
    cutoff = config.MAX_DISTANCE if max_distance is None else max_distance
    pool = max(RERANK_POOL, k)

    vec_rows = _vector_hits(question, pool, doc_id) if mode in ("vector", "hybrid") else []
    kw_rows = _keyword_hits(question, pool, doc_id) if mode in ("keyword", "hybrid") else []

    by_id = {r["id"]: r for r in vec_rows}
    by_id.update({r["id"]: r for r in kw_rows})

    if mode == "keyword":
        fused = [(r["id"], float(r["hits"])) for r in kw_rows]
    elif mode == "vector":
        fused = [(r["id"], float(1.0 - r["distance"])) for r in vec_rows]
    else:
        scores = _rrf(vec_rows, kw_rows)
        fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    hits: list[dict] = []
    for cid, _score in fused:
        row = by_id.get(cid)
        if not row:
            continue
        distance = float(row["distance"]) if row.get("distance") is not None else None
        # 向量模式与混合模式下，距离过远视为不相关；关键词模式不设距离门槛
        if mode != "keyword" and distance is not None and distance > cutoff:
            continue
        hits.append(
            {
                "chunk_id": cid,
                "doc_id": row["doc_id"],
                "doc_name": row["doc_name"],
                "seq": row["seq"],
                "content": row["content"],
                "distance": round(distance, 4) if distance is not None else None,
                "score": round(1.0 - distance, 4) if distance is not None else None,
                "hits": row.get("hits"),
            }
        )
        if len(hits) >= k:
            break

    return hits
