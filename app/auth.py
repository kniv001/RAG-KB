"""HTTP Basic 认证。

凭据来源优先级：
  1. 环境变量 KB_USER / KB_PASS
  2. data/auth.json
  3. 都没有 -> 自动生成随机密码并写入 data/auth.json（data/ 已 gitignore）

改密码：编辑 data/auth.json，或设环境变量后重启。
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

CRED_FILE = Path(__file__).resolve().parent.parent / "data" / "auth.json"
REALM = "RAG KB"


def _load_credentials() -> tuple[str, str]:
    user = os.environ.get("KB_USER")
    password = os.environ.get("KB_PASS")
    if user and password:
        return user, password

    if CRED_FILE.exists():
        data = json.loads(CRED_FILE.read_text("utf-8"))
        return data["user"], data["password"]

    user = "kniv"
    password = secrets.token_urlsafe(18)
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(
        json.dumps({"user": user, "password": password}, ensure_ascii=False, indent=2),
        "utf-8",
    )
    print(f"[auth] generated initial credentials -> {CRED_FILE}", flush=True)
    print(f"[auth] user={user}  password={password}", flush=True)
    return user, password


USER, PASSWORD = _load_credentials()


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """全站保护：未认证一律 401。

    例外：/api/whoami 免认证 —— 它只回显调用方自己的 IP 和协议栈，
    不含知识库任何内容，但用于排查"某设备连不上"时必须能在登录前打开。
    """

    PUBLIC_PATHS = {"/api/whoami"}

    async def dispatch(self, request, call_next):
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
                got_user, _, got_pass = raw.partition(":")
            except Exception:
                got_user = got_pass = ""
            if hmac.compare_digest(got_user, USER) and hmac.compare_digest(got_pass, PASSWORD):
                return await call_next(request)

        return PlainTextResponse(
            "401 Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
        )
