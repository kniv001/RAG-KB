"""个人 RAG 知识库 · HTTP 路由层

这一层只做 HTTP 收发与错误转换，业务逻辑全在 app/pipeline.py、app/chat.py
及各阶段模块里。想改行为请改那些模块，不要往这里堆逻辑。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from starlette.middleware.base import BaseHTTPMiddleware

from app import (auth, cache, chat, config, db, embed, pipeline, providers,
                 rstore, retrieve, security, settings, store, tasks, users)
from app.auth import TokenAuthMiddleware

app = FastAPI(title="RAG KB", version="0.6.0")
app.add_middleware(TokenAuthMiddleware)

config.UPLOADS.mkdir(parents=True, exist_ok=True)


class NoStoreMiddleware(BaseHTTPMiddleware):
    """/api/* 一律禁止缓存。

    内容是私有的（Basic 认证之后），绝不能让 Cloudflare 边缘或浏览器把
    问答结果缓存下来 —— 那既可能串号，也可能在资料更新后返回陈旧答案。
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(NoStoreMiddleware)


@app.on_event("startup")
def _startup() -> None:
    try:
        db.init_schema()
        settings.load()
        n = tasks.reap_orphans()
        users.migrate_legacy()
        print(f"[db] schema ready (reaped {n} orphan task(s))", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[db] startup failed: {type(exc).__name__}: {exc}", flush=True)


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, (pipeline.PipelineError, ValueError)):
        return HTTPException(400, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(400, str(exc))
    return HTTPException(500, f"{type(exc).__name__}: {exc}")


# ---------------- 页面与健康 ----------------

@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text("utf-8")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "rag-kb",
        "version": app.version,
        "db": db.health(),
        "redis": rstore.health(),
        "embed": embed.check(),
        "defaults": settings.load()["default_chat"],
        "stale": store.stale_documents(),
    }


# ---------------- 设置与模型提供方 ----------------

class DefaultsBody(BaseModel):
    chat_provider: str | None = None
    chat_model: str | None = None
    embed_provider: str | None = None
    embed_model: str | None = None
    chat_temperature: float | None = Field(default=None, ge=0, le=2)


@app.get("/api/auth/config")
def auth_config() -> dict:
    """登录页需要知道的公开信息（不含任何凭据）。"""
    return {
        "ok": True,
        "access_ttl": security.ACCESS_TTL,
        "refresh_ttl": security.REFRESH_TTL,
        "has_user": users.count() > 0,
        "redis": rstore.health().get("ok", False),
    }


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


@app.post("/api/auth/login")
def api_login(body: LoginBody, request: Request, response: Response) -> dict:
    try:
        result = auth.login(body.username, body.password, request.headers.get("user-agent", ""))
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except rstore.StoreError as exc:
        raise HTTPException(503, f"会话存储不可用：{exc}") from exc
    token = result.pop("refresh_token")
    auth.set_refresh_cookie(response, token, auth.cookie_secure(request))
    return {"ok": True, **result}


@app.post("/api/auth/refresh")
def api_refresh(request: Request, response: Response) -> dict:
    try:
        result = auth.refresh(
            request.cookies.get(auth.COOKIE_NAME), request.headers.get("user-agent", "")
        )
    except auth.AuthError as exc:
        auth.clear_refresh_cookie(response)
        raise HTTPException(401, str(exc)) from exc
    token = result.pop("refresh_token")
    auth.set_refresh_cookie(response, token, auth.cookie_secure(request))
    return {"ok": True, **result}


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response) -> dict:
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    auth.clear_refresh_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request) -> dict:
    username = getattr(request.state, "user", None)
    user = users.get(username) if username else None
    if not user:
        raise HTTPException(401, "未登录")
    return {
        "ok": True,
        "username": user["username"],
        "created_at": user["created_at"].isoformat(timespec="seconds"),
        "last_login_at": user["last_login_at"].isoformat(timespec="seconds")
        if user.get("last_login_at")
        else None,
        "sessions": rstore.count_user_sessions(username),
    }


@app.post("/api/auth/password")
def api_change_password(body: PasswordBody, request: Request) -> dict:
    username = getattr(request.state, "user", None)
    if not username:
        raise HTTPException(401, "未登录")
    try:
        revoked = auth.change_password(username, body.old_password, body.new_password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "revoked_sessions": revoked,
        "note": "密码已更新，所有刷新令牌已吊销。当前 access token 仍有效至自然过期（≤30 分钟）。",
    }


@app.get("/api/whoami")
def whoami(request: Request) -> dict:
    """自报家门：告诉调用方它这次是用 IPv4 还是 IPv6 连进来的。

    排查"某设备连不上"时非常有用 —— 浏览器/系统会按 RFC 6724 优先选 IPv6，
    只要这个接口能打开，就能一眼看出它实际走了哪条协议栈。
    Cloudflare 会带上 CF-Connecting-IP（真实客户端 IP），
    经过隧道时 request.client.host 只会是 127.0.0.1，所以优先取它。
    """
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    via_tunnel = bool(request.headers.get("cf-connecting-ip"))
    return {
        "ok": True,
        "client_ip": ip,
        "family": "IPv6" if ":" in ip else "IPv4",
        "scheme": request.headers.get("x-forwarded-proto"),
        "country": request.headers.get("cf-ipcountry"),
        "host": request.headers.get("host"),
        "via_cloudflare": via_tunnel,
        "user_agent": request.headers.get("user-agent", "")[:150],
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return {"ok": True, **settings.public()}


@app.put("/api/settings/defaults")
def put_defaults(body: DefaultsBody) -> dict:
    cfg = settings.load(force=True)
    if body.chat_provider and body.chat_model:
        cfg["default_chat"] = {"provider": body.chat_provider, "model": body.chat_model}
    if body.embed_provider and body.embed_model:
        cfg["default_embed"] = {"provider": body.embed_provider, "model": body.embed_model}
    if body.chat_temperature is not None:
        cfg["chat_temperature"] = body.chat_temperature
    settings.save(cfg)
    return {"ok": True, **settings.public()}


@app.get("/api/providers/{provider}/probe")
def probe_provider(provider: str) -> dict:
    return providers.probe(provider)


# ---------------- 文档 ----------------

@app.get("/api/docs")
def list_docs() -> dict:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, suffix, bytes, uploaded_at, status, chunk_count, embed_model"
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
    if suffix not in config.ALLOWED_SUFFIX:
        raise HTTPException(415, f"不支持的格式 {suffix}，允许：{sorted(config.ALLOWED_SUFFIX)}")

    doc_id = uuid.uuid4().hex[:12]
    dest = config.UPLOADS / f"{doc_id}{suffix}"
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"超过上限 {config.MAX_UPLOAD_BYTES // 1024 // 1024} MB")
            fh.write(chunk)

    rel = str(dest.relative_to(config.ROOT)).replace("\\", "/")
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


@app.post("/api/docs/{doc_id}/index", status_code=202)
def index_doc(doc_id: str) -> dict:
    """建索引是耗时操作（大文档可超百秒），必须异步 ——
    Cloudflare 免费版对源站有 100 秒硬超时，同步接口会被 524 掐断。
    这里立即返回 202 + 任务 id，前端轮询 /api/tasks/{tid}。"""
    doc = store.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    tid = tasks.submit(doc_id, lambda report: pipeline.index_document(doc_id, report))
    return {"ok": True, "accepted": True, "task_id": tid, "doc_id": doc_id,
            "poll": f"/api/tasks/{tid}"}


@app.get("/api/tasks/{tid}")
def get_task(tid: str) -> dict:
    t = tasks.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    for f in ("created_at", "updated_at"):
        if t.get(f):
            t[f] = t[f].isoformat(timespec="seconds")
    return {"ok": True, "task": t}


@app.get("/api/docs/{doc_id}/task")
def get_doc_task(doc_id: str) -> dict:
    t = tasks.latest_for(doc_id)
    if t:
        for f in ("created_at", "updated_at"):
            if t.get(f):
                t[f] = t[f].isoformat(timespec="seconds")
    return {"ok": True, "task": t}


# ---------------- 缓存运维 ----------------

@app.get("/api/cache/stats")
def cache_stats() -> dict:
    s = cache.stats()
    for v in s.values():
        if v.get("last"):
            v["last"] = v["last"].isoformat(timespec="seconds")
    return {"ok": True, "stats": s}


@app.delete("/api/cache")
def cache_clear(which: str | None = None) -> dict:
    if which and which not in ("embeddings", "answers", "parses"):
        raise HTTPException(400, "which 只能是 embeddings / answers / parses，或留空全清")
    return {"ok": True, "removed": cache.clear(which)}


@app.get("/api/docs/{doc_id}/chunks")
def doc_chunks(doc_id: str, limit: int = 20) -> dict:
    return {"doc_id": doc_id, "chunks": store.chunk_preview(doc_id, limit)}


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
    (config.ROOT / row["stored_path"]).unlink(missing_ok=True)
    return {"ok": True, "removed": doc_id}


# ---------------- 检索 / 问答 / 对话 ----------------

class QueryBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    mode: str = Field(default=retrieve.MODE_DEFAULT, pattern="^(vector|keyword|hybrid)$")
    doc_id: str | None = None
    provider: str | None = None
    model: str | None = None


class ChatBody(QueryBody):
    conv_id: str | None = None


@app.post("/api/search")
def api_search(body: QueryBody) -> dict:
    """只检索不生成，也支持按文档过滤与三种检索模式 —— 调检索时用这个。"""
    try:
        hits = retrieve.search(
            body.question, top_k=body.top_k, mode=body.mode, doc_id=body.doc_id
        )
        return {
            "ok": True,
            "question": body.question,
            "mode": body.mode,
            "sources": [
                {
                    "doc_id": h["doc_id"],
                    "doc_name": h["doc_name"],
                    "seq": h["seq"],
                    "score": h["score"],
                    "distance": h["distance"],
                    "hits": h["hits"],
                    "content": h["content"],
                }
                for h in hits
            ],
        }
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@app.post("/api/ask")
def api_ask(body: QueryBody) -> dict:
    """无状态问答（不写对话库）。"""
    try:
        return {
            "ok": True,
            **pipeline.ask(
                body.question, body.provider, body.model, body.top_k, body.mode
            ),
        }
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@app.post("/api/chat")
def api_chat(body: ChatBody) -> dict:
    """多轮对话：不传 conv_id 则新建会话，传入则接续。"""
    try:
        return {
            "ok": True,
            **chat.ask(
                body.question,
                conv_id=body.conv_id,
                provider=body.provider,
                model=body.model,
                top_k=body.top_k,
                mode=body.mode,
            ),
        }
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@app.get("/api/conversations")
def api_conversations(limit: int = 50) -> dict:
    rows = chat.listing(limit)
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat(timespec="seconds")
    return {"count": len(rows), "conversations": rows}


@app.get("/api/conversations/{cid}")
def api_conversation(cid: str) -> dict:
    conv = chat.get(cid)
    if not conv:
        raise HTTPException(404, "会话不存在")
    msgs = chat.messages(cid)
    for m in msgs:
        m["created_at"] = m["created_at"].isoformat(timespec="seconds")
    conv["created_at"] = conv["created_at"].isoformat(timespec="seconds")
    conv["updated_at"] = conv["updated_at"].isoformat(timespec="seconds")
    return {"ok": True, "conversation": conv, "messages": msgs}


@app.delete("/api/conversations/{cid}")
def api_delete_conversation(cid: str) -> dict:
    if not chat.remove(cid):
        raise HTTPException(404, "会话不存在")
    return {"ok": True, "removed": cid}
