"""PostgreSQL + pgvector 存储层。

连接参数优先级：环境变量 > 仓库内默认值。
  KB_DB_HOST / KB_DB_PORT / KB_DB_NAME / KB_DB_USER / KB_DB_PASSWORD

向量维度与当前向量模型由 data/settings.json 决定（settings.embed_dim / default_embed）。
chunks.embed_model 记录每块是用哪个模型生成的 —— 不同模型的向量空间不可比较，
检索时按当前模型过滤，切换模型后旧块自动失效（需重建索引），避免静默返回垃圾结果。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app import settings

ROOT = Path(__file__).resolve().parent.parent


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
    return int(settings.load().get("embed_dim", 1024))


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
    chunk_count integer NOT NULL DEFAULT 0,
    embed_model text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id          bigserial PRIMARY KEY,
    doc_id      text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    content     text NOT NULL,
    embedding   vector({dim}),
    embed_model text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_id, seq)
);

-- 兼容已存在的库
ALTER TABLE documents ADD COLUMN IF NOT EXISTS embed_model text NOT NULL DEFAULT '';
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS embed_model text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS chunks_doc_idx   ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_model_idx ON chunks (embed_model);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS conversations (
    id         text PRIMARY KEY,
    title      text NOT NULL DEFAULT '新对话',
    provider   text,
    model      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id         bigserial PRIMARY KEY,
    conv_id    text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role       text NOT NULL,
    content    text NOT NULL,
    sources    jsonb,
    provider   text,
    model      text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages (conv_id, id);

CREATE TABLE IF NOT EXISTS index_tasks (
    id         text PRIMARY KEY,
    doc_id     text NOT NULL,
    kind       text NOT NULL DEFAULT 'index',
    status     text NOT NULL DEFAULT 'running',   -- running | done | error
    progress   integer NOT NULL DEFAULT 0,
    total      integer NOT NULL DEFAULT 0,
    message    text,
    result     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS index_tasks_doc_idx ON index_tasks (doc_id, created_at DESC);

-- ---------- 缓存三表 ----------
-- 向量缓存刻意用 text 存而非 vector 类型：缓存只按 key 精确查，从不做相似度检索，
-- 用 text 就不被 embed_dim 的 DDL 绑死，将来换维度不同的向量模型无需迁移缓存表。

CREATE TABLE IF NOT EXISTS cache_embeddings (
    key         text PRIMARY KEY,
    model       text NOT NULL,
    vec         text NOT NULL,
    dim         integer NOT NULL,
    hits        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);

CREATE TABLE IF NOT EXISTS cache_answers (
    key         text PRIMARY KEY,
    question    text NOT NULL,
    answer      text NOT NULL,
    provider    text,
    model       text,
    sources     jsonb,
    hits        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);

CREATE TABLE IF NOT EXISTS cache_parses (
    key         text PRIMARY KEY,
    name        text,
    text        text NOT NULL,
    chars       integer NOT NULL,
    hits        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);
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
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ext = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM documents")
            docs = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM chunks")
            chunks = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM conversations")
            convs = cur.fetchone()["n"]
        return {
            "ok": True,
            "server": str(row["v"]).split(" on ")[0],
            "database": row["db"],
            "pgvector": ext["extversion"] if ext else None,
            "embed_dim": embed_dim(),
            "documents": docs,
            "chunks": chunks,
            "conversations": convs,
        }
    except Exception as exc:  # noqa: BLE001 - 健康检查不应抛
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
