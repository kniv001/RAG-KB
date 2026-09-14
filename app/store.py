"""分块与向量的持久化。

向量以字面量 `[a,b,c]` 形式写入并显式 `::vector` 转型 —— 不依赖 numpy，
也不依赖 pgvector 的 Python 适配器，任何环境都能跑。

每块都记录 embed_model（形如 "local:bge-m3"）。不同模型的向量空间不可比较，
检索时按当前模型过滤，切换模型后旧块自动失效而不是返回垃圾结果。
"""

from __future__ import annotations

from app import db, embed

_INSERT = """
INSERT INTO chunks (doc_id, seq, content, embedding, embed_model)
VALUES (%s, %s, %s, %s::vector, %s)
"""


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def replace_chunks(doc_id: str, chunks: list[str], vectors: list[list[float]]) -> int:
    """整篇替换：先清旧块，再写新块，最后回写文档状态。"""
    if len(chunks) != len(vectors):
        raise ValueError(f"分块数({len(chunks)}) 与向量数({len(vectors)}) 不一致")

    model = embed.current_model()
    rows = [
        (doc_id, i, text, _literal(vec), model)
        for i, (text, vec) in enumerate(zip(chunks, vectors))
    ]

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
        if rows:
            cur.executemany(_INSERT, rows)
        cur.execute(
            "UPDATE documents SET chunk_count = %s, status = 'indexed', embed_model = %s"
            " WHERE id = %s",
            (len(rows), model, doc_id),
        )
    return len(rows)


def clear_chunks(doc_id: str) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
        cur.execute(
            "UPDATE documents SET chunk_count = 0, status = 'stored', embed_model = ''"
            " WHERE id = %s",
            (doc_id,),
        )


def get_document(doc_id: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE id = %s", (doc_id,))
        return cur.fetchone()


def chunk_preview(doc_id: str, limit: int = 20) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT seq, content, length(content) AS chars, embed_model FROM chunks"
            " WHERE doc_id = %s ORDER BY seq LIMIT %s",
            (doc_id, limit),
        )
        return cur.fetchall()


def stale_documents() -> list[dict]:
    """已索引但用的不是当前向量模型的文档 —— 需要重建索引。"""
    current = embed.current_model()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, embed_model, chunk_count FROM documents"
            " WHERE chunk_count > 0 AND embed_model <> %s",
            (current,),
        )
        return cur.fetchall()
