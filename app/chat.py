"""对话：会话与消息的持久化，多轮上下文拼装。

会话和消息都存在 PostgreSQL 里（conversations / messages 两表）。
每条助手消息会把当轮召回的来源以 jsonb 存下来，便于回溯"这句话是从哪来的"。

魔改点：
  · 想加"会话标题自动生成"：在 append 完第一条用户消息后调一次 providers.chat
  · 想加"多轮查询改写"：在 answer() 前用 history 把问题改写成独立查询再检索
  · 想换记忆策略（摘要压缩 / 只留最近 N 轮）：改 history()
"""

from __future__ import annotations

import uuid

from app import db, pipeline, retrieve


def create(title: str | None = None, provider: str | None = None, model: str | None = None) -> dict:
    cid = uuid.uuid4().hex[:12]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, title, provider, model) VALUES (%s, %s, %s, %s)"
            " RETURNING id, title, created_at",
            (cid, (title or "新对话")[:80], provider, model),
        )
        return cur.fetchone()


def touch(cid: str, provider: str | None = None, model: str | None = None) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE conversations SET updated_at = now(),"
            " provider = COALESCE(%s, provider), model = COALESCE(%s, model)"
            " WHERE id = %s",
            (provider, model, cid),
        )


def get(cid: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM conversations WHERE id = %s", (cid,))
        return cur.fetchone()


def listing(limit: int = 50) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.title, c.provider, c.model, c.updated_at,"
            " (SELECT count(*) FROM messages m WHERE m.conv_id = c.id) AS turns"
            " FROM conversations c ORDER BY c.updated_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def remove(cid: str) -> bool:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM conversations WHERE id = %s RETURNING id", (cid,))
        return cur.fetchone() is not None


def messages(cid: str, limit: int = 200) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, role, content, sources, provider, model, created_at"
            " FROM messages WHERE conv_id = %s ORDER BY id LIMIT %s",
            (cid, limit),
        )
        return cur.fetchall()


def history(cid: str, limit: int = 16) -> list[dict]:
    """给模型看的最近若干轮（只取 role/content）。"""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT role, content FROM messages WHERE conv_id = %s ORDER BY id DESC LIMIT %s",
            (cid, limit),
        )
        rows = cur.fetchall()
    return list(reversed(rows))


def append(
    cid: str,
    role: str,
    content: str,
    sources: list | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    import json

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conv_id, role, content, sources, provider, model)"
            " VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
            (cid, role, content, json.dumps(sources, ensure_ascii=False) if sources else None,
             provider, model),
        )


def ask(
    question: str,
    conv_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    mode: str = retrieve.MODE_DEFAULT,
) -> dict:
    """一轮对话：检索 -> 生成 -> 落库。conv_id 为空则新建会话。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("问题为空")

    if conv_id:
        conv = get(conv_id)
        if not conv:
            raise ValueError(f"会话不存在：{conv_id}")
        hist = history(conv_id)
    else:
        conv = create(title=question[:30], provider=provider, model=model)
        conv_id = conv["id"]
        hist = []

    hits = retrieve.search(question, top_k=top_k, mode=mode)

    if hits:
        g = pipeline.answer_with_cache(
            question, hits, history=hist, provider=provider, model=model
        )
        answer, used_p, used_m = g["content"], g["provider"], g["model"]
        sources, cached = g["sources"], g["cached"]
    else:
        answer, used_p, used_m = "资料中没有相关内容。", provider, model
        sources, cached = [], False

    append(conv_id, "user", question)
    append(conv_id, "assistant", answer, sources=sources, provider=used_p, model=used_m)
    touch(conv_id, provider=used_p, model=used_m)

    return {
        "conv_id": conv_id,
        "question": question,
        "answer": answer,
        "provider": used_p,
        "model": used_m,
        "sources": sources,
        "cached": cached,
    }
