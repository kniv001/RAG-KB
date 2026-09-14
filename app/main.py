"""个人 RAG 知识库 · 应用

当前阶段：文档上传入库（PostgreSQL + pgvector），切分/向量化/检索待接。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app import db
from app.auth import BasicAuthMiddleware

BASE = Path(__file__).resolve().parent.parent
UPLOADS = BASE / "data" / "uploads"

UPLOADS.mkdir(parents=True, exist_ok=True)

ALLOWED_SUFFIX = {".txt", ".md", ".markdown", ".pdf", ".docx", ".csv", ".json", ".html"}
MAX_BYTES = 50 * 1024 * 1024

app = FastAPI(title="RAG KB", version="0.2.0")
app.add_middleware(BasicAuthMiddleware)


@app.on_event("startup")
def _startup() -> None:
    try:
        db.init_schema()
        print("[db] schema ready", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[db] schema init failed: {type(exc).__name__}: {exc}", flush=True)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (BASE / "web" / "index.html").read_text("utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "rag-kb", "version": app.version, "db": db.health()}


@app.get("/api/docs")
def list_docs() -> dict:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, suffix, bytes, uploaded_at, status, chunk_count"
                " FROM documents ORDER BY uploaded_at"
            )
            docs = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"数据库不可用：{exc}") from exc
    for d in docs:
        d["uploaded_at"] = d["uploaded_at"].isoformat(timespec="seconds")
    return {"count": len(docs), "docs": docs}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if not name:
        raise HTTPException(400, "缺少文件名")
    if suffix not in ALLOWED_SUFFIX:
        raise HTTPException(415, f"不支持的格式 {suffix}，允许：{sorted(ALLOWED_SUFFIX)}")

    doc_id = uuid.uuid4().hex[:12]
    dest = UPLOADS / f"{doc_id}{suffix}"
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"超过上限 {MAX_BYTES // 1024 // 1024} MB")
            fh.write(chunk)

    rel = str(dest.relative_to(BASE)).replace("\\", "/")
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (id, name, suffix, bytes, stored_path)"
                " VALUES (%s, %s, %s, %s, %s)",
                (doc_id, name, suffix, size, rel),
            )
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(503, f"数据库不可用：{exc}") from exc

    return {"ok": True, "doc": {"id": doc_id, "name": name, "bytes": size, "status": "stored"}}


@app.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: str) -> dict:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE id = %s RETURNING stored_path", (doc_id,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"数据库不可用：{exc}") from exc
    if not row:
        raise HTTPException(404, "文档不存在")
    (BASE / row["stored_path"]).unlink(missing_ok=True)
    return {"ok": True, "removed": doc_id}
