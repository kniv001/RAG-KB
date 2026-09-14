"""PostgreSQL + pgvector 存储层。

连接参数优先级：环境变量 > 仓库内默认值。
  KB_DB_HOST / KB_DB_PORT / KB_DB_NAME / KB_DB_USER / KB_DB_PASSWORD
  KB_EMBED_DIM  向量维度（默认 1024，对应 bge-m3；改维度需重建表）

凭据文件：data/pgapp.txt（首次配置时生成，已 gitignore）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EMBED_DIM = 1024


def _password() -> str:
    if pw := os.environ.get("KB_DB_PASSWORD"):
        return pw
    f = ROOT / "data" / "pgapp.txt"
    return f.read_text("ascii").strip() if f.exists() else ""


def conninfo() -> str:
    return " ".join(
        [
            f"host={os.environ.get('KB_DB_HOST', '127.0.0.1')}",
            f"port={os.environ.get('KB_DB_PORT', '5432')}",
            f"dbname={os.environ.get('KB_DB_NAME', 'ragkb')}",
            f"user={os.environ.get('KB_DB_USER', 'ragkb')}",
            f"password={_password()}",
            "connect_timeout=5",
        ]
    )


def embed_dim() -> int:
    return int(os.environ.get("KB_EMBED_DIM", DEFAULT_EMBED_DIM))


def connect() -> psycopg.Connection:
    return psycopg.connect(conninfo(), row_factory=dict_row)


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    suffix      text NOT NULL,
    bytes       bigint NOT NULL,
    stored_path text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL DEFAULT 'stored',
    chunk_count integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id          bigserial PRIMARY KEY,
    doc_id      text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    content     text NOT NULL,
    embedding   vector({dim}),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_id, seq)
);

CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
"""


def init_schema() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA.format(dim=embed_dim()))


def health() -> dict[str, Any]:
    """返回数据库状态，供 /api/health 使用；失败不抛异常。"""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT version() AS v, current_database() AS db")
            row = cur.fetchone()
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            ext = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM documents")
            docs = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM chunks")
            chunks = cur.fetchone()["n"]
        return {
            "ok": True,
            "server": str(row["v"]).split(" on ")[0],
            "database": row["db"],
            "pgvector": ext["extversion"] if ext else None,
            "embed_dim": embed_dim(),
            "documents": docs,
            "chunks": chunks,
        }
    except Exception as exc:  # noqa: BLE001 - 健康检查不应抛
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
