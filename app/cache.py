"""三层缓存：向量 / 解析 / 回答。全部落在 PostgreSQL，重启不丢。

设计要点：
  1. 键一律是内容哈希，**且把影响结果的参数都放进键里**（模型名、上下文哈希、历史哈希）。
     这样"改了输入就自动不命中"，不需要任何手工失效逻辑 —— 缓存最大的坑就是返回陈旧结果。
  2. 向量缓存用 text 存而非 vector 类型：只按 key 精确查，不做相似度检索，
     因此不被 embed_dim 的 DDL 绑死，换维度模型时无需迁移。
  3. 每条都记 hits / last_hit_at，便于判断哪些缓存真有用、可以清。

魔改点：
  · 想加 TTL：在 get_* 里加上 created_at > now() - interval 的判断
  · 想加容量上限：定期跑 clear() 或按 last_hit_at 淘汰
  · 想缓存别的东西（比如查询改写结果）：照抄 get/put 的模式加一对函数
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app import db

SEP = "\x00"


def key(*parts: Any) -> str:
    """把若干参数拼成稳定的哈希键。None 与空串要区分开，所以显式标注。"""
    joined = SEP.join("N" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------- 向量缓存 ----------------

def get_embeddings(texts: list[str], model: str) -> tuple[dict[int, list[float]], list[int]]:
    """返回 ({下标: 向量}, [未命中的下标])。一次查库，避免逐条往返。"""
    if not texts:
        return {}, []

    keys = [key(model, t) for t in texts]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT key, vec FROM cache_embeddings WHERE key = ANY(%s)", (keys,)
        )
        found = {r["key"]: r["vec"] for r in cur.fetchall()}
        if found:
            cur.execute(
                "UPDATE cache_embeddings SET hits = hits + 1, last_hit_at = now()"
                " WHERE key = ANY(%s)",
                (list(found.keys()),),
            )

    hits: dict[int, list[float]] = {}
    misses: list[int] = []
    for i, k in enumerate(keys):
        raw = found.get(k)
        if raw is None:
            misses.append(i)
        else:
            hits[i] = json.loads(raw)
    return hits, misses


def put_embeddings(texts: list[str], vectors: list[list[float]], model: str) -> None:
    if not texts:
        return
    rows = [
        (key(model, t), model, json.dumps(v), len(v))
        for t, v in zip(texts, vectors)
    ]
    with db.connect() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO cache_embeddings (key, model, vec, dim) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (key) DO NOTHING",
            rows,
        )


# ---------------- 解析缓存 ----------------

def get_parse(file_hash: str) -> str | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cache_parses SET hits = hits + 1, last_hit_at = now()"
            " WHERE key = %s RETURNING text",
            (file_hash,),
        )
        row = cur.fetchone()
        return row["text"] if row else None


def put_parse(file_hash: str, name: str, text: str) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cache_parses (key, name, text, chars) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (key) DO NOTHING",
            (file_hash, name, text, len(text)),
        )


# ---------------- 回答缓存 ----------------

def context_hash(chunks: list[str]) -> str:
    """上下文变了，答案就该重算 —— 这是回答缓存不返回陈旧内容的关键。"""
    return text_key(SEP.join(chunks))


def history_hash(history: list[dict] | None) -> str:
    if not history:
        return ""
    return text_key(SEP.join(f"{h.get('role')}:{h.get('content')}" for h in history))


def answer_key(
    question: str,
    ctx_hash: str,
    hist_hash: str,
    provider: str,
    model: str,
    temperature: float,
) -> str:
    return key(question, ctx_hash, hist_hash, provider, model, temperature)


def get_answer(k: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cache_answers SET hits = hits + 1, last_hit_at = now()"
            " WHERE key = %s RETURNING answer, provider, model, sources",
            (k,),
        )
        return cur.fetchone()


def put_answer(
    k: str, question: str, answer: str, provider: str, model: str, sources: list
) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cache_answers (key, question, answer, provider, model, sources)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (key) DO NOTHING",
            (k, question, answer, provider, model, json.dumps(sources, ensure_ascii=False)),
        )


# ---------------- 运维 ----------------

_TABLES = {
    "embeddings": "cache_embeddings",
    "answers": "cache_answers",
    "parses": "cache_parses",
}


def stats() -> dict:
    out: dict[str, Any] = {}
    with db.connect() as conn, conn.cursor() as cur:
        for name, table in _TABLES.items():
            cur.execute(
                f"SELECT count(*) AS n, coalesce(sum(hits), 0) AS hits,"
                f" max(last_hit_at) AS last FROM {table}"  # noqa: S608 - 表名来自白名单
            )
            out[name] = cur.fetchone()
    return out


def clear(which: str | None = None) -> dict:
    """清空缓存。which 为空则全清，否则只清 embeddings / answers / parses。"""
    targets = _TABLES if not which else {which: _TABLES[which]}
    removed = {}
    with db.connect() as conn, conn.cursor() as cur:
        for name, table in targets.items():
            cur.execute(f"DELETE FROM {table}")  # noqa: S608 - 表名来自白名单
            removed[name] = cur.rowcount
    return removed
