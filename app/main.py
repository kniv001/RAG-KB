"""个人 RAG 知识库 · 应用骨架

Step 1 目标：验证「外网浏览器 → Cloudflare 隧道 → 本机 FastAPI」链路。
本文件只做上传收件与登记，尚未接入切分/向量化/检索。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.auth import BasicAuthMiddleware

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
INDEX = DATA / "docs.json"

UPLOADS.mkdir(parents=True, exist_ok=True)

ALLOWED_SUFFIX = {".txt", ".md", ".markdown", ".pdf", ".docx", ".csv", ".json", ".html"}
MAX_BYTES = 50 * 1024 * 1024

app = FastAPI(title="RAG KB", version="0.1.0")
app.add_middleware(BasicAuthMiddleware)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_docs() -> list[dict]:
    if INDEX.exists():
        return json.loads(INDEX.read_text("utf-8"))
    return []


def save_docs(docs: list[dict]) -> None:
    INDEX.write_text(json.dumps(docs, ensure_ascii=False, indent=2), "utf-8")


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (BASE / "web" / "index.html").read_text("utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "rag-kb", "version": app.version, "time": now()}


@app.get("/api/docs")
def list_docs() -> dict:
    docs = load_docs()
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

    doc = {
        "id": doc_id,
        "name": name,
        "suffix": suffix,
        "bytes": size,
        "stored": str(dest.relative_to(BASE)).replace("\\", "/"),
        "uploaded_at": now(),
        "status": "stored",  # 后续：chunked → embedded → indexed
    }
    docs = load_docs()
    docs.append(doc)
    save_docs(docs)
    return {"ok": True, "doc": doc}


@app.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: str) -> dict:
    docs = load_docs()
    keep = [d for d in docs if d["id"] != doc_id]
    if len(keep) == len(docs):
        raise HTTPException(404, "文档不存在")
    for d in docs:
        if d["id"] == doc_id:
            (BASE / d["stored"]).unlink(missing_ok=True)
    save_docs(keep)
    return {"ok": True, "removed": doc_id}
