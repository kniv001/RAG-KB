"""认证：JWT Access Token + Redis Refresh Token。

取代了早期的全站 HTTP Basic（浏览器弹框、凭据是明文文件）。现在：

  Access Token  JWT(HS256)，30 分钟，放 Authorization: Bearer 头
                无状态 —— 每个请求只验签，不查库不查 Redis
  Refresh Token 32 字节随机串，30 天，存 Redis，以 httpOnly Cookie 下发
                可主动吊销 → 登出/改密码/踢下线才真正生效
                每次刷新都轮换，旧 token 立即失效

为什么 Refresh 用 httpOnly Cookie 而不是 localStorage：
  localStorage 里的令牌能被任何 XSS 读走；httpOnly Cookie JS 读不到。
  再配 SameSite=Strict，跨站请求不会带上它，顺带免疫 CSRF。

Cookie 的 Secure 标志按请求协议自动决定（看 X-Forwarded-Proto）：
  经 Cloudflare 是 https → 带 Secure；本机 http://127.0.0.1 调试 → 不带，否则浏览器不发送。
"""

from __future__ import annotations

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app import rstore, security, users

COOKIE_NAME = "kb_refresh"
COOKIE_PATH = "/api/auth"

# 不需要令牌即可访问的路径
PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/api/whoami",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/config",
}


def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if not path.startswith("/api/"):
        return True          # 前端静态资源
    return False


class AuthError(RuntimeError):
    """登录/刷新/改密失败。路由层统一转成 401 或 429。"""


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """/api/* 一律要求有效的 Bearer 令牌。"""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if is_public(path):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return _unauthorized("缺少 Bearer 令牌")

        token = header[7:].strip()
        try:
            payload = security.decode_access_token(token)
        except jwt.ExpiredSignatureError:
            return _unauthorized("令牌已过期", expired=True)
        except jwt.InvalidTokenError:
            return _unauthorized("令牌无效")

        request.state.user = payload.get("sub")
        return await call_next(request)


def _unauthorized(message: str, expired: bool = False) -> JSONResponse:
    """401 带 WWW-Authenticate: Bearer，并把 expired 明确告诉前端，
    让前端知道该去刷新还是该跳登录页。"""
    body = {"detail": message, "expired": expired}
    headers = {"WWW-Authenticate": 'Bearer realm="rag-kb"'}
    if expired:
        headers["X-Token-Expired"] = "1"
    return JSONResponse(body, status_code=401, headers=headers)


# ---------------- Cookie ----------------

def cookie_secure(request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


def set_refresh_cookie(response, token: str, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=security.REFRESH_TTL,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=COOKIE_PATH,
    )


def clear_refresh_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)


# ---------------- 流程 ----------------

def login(username: str, password: str, user_agent: str = "") -> dict:
    """成功返回 {access_token, expires_in, refresh_token, user}。失败抛 AuthError。"""
    locked = rstore.login_locked(username)
    if locked:
        raise AuthError(f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")

    user = users.verify(username, password)
    if not user:
        n = rstore.note_login_fail(username)
        left = max(0, rstore.MAX_LOGIN_FAILS - n)
        raise AuthError(f"用户名或密码错误（还可尝试 {left} 次）" if left else "失败次数过多，账号已临时锁定")

    rstore.clear_login_fail(username)
    users.touch_login(username)

    access, ttl = security.make_access_token(username)
    refresh = security.new_refresh_token()
    rstore.save_refresh(refresh, username, user_agent)

    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": ttl,
        "refresh_token": refresh,
        "user": {"username": username},
    }


def refresh(old_token: str | None, user_agent: str = "") -> dict:
    """用 Cookie 里的 refresh 换新 access，并轮换 refresh。"""
    if not old_token:
        raise AuthError("缺少刷新令牌")
    data = rstore.get_refresh(old_token)
    if not data:
        raise AuthError("刷新令牌无效或已过期")

    username = data["user"]
    if not users.get(username):
        rstore.delete_refresh(old_token)
        raise AuthError("账号不存在")

    rstore.delete_refresh(old_token)          # 轮换：旧的立即作废
    access, ttl = security.make_access_token(username)
    new_token = security.new_refresh_token()
    rstore.save_refresh(new_token, username, user_agent)

    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": ttl,
        "refresh_token": new_token,
        "user": {"username": username},
    }


def logout(token: str | None) -> None:
    if token:
        rstore.delete_refresh(token)


def change_password(username: str, old: str, new: str) -> int:
    """改密码并吊销该用户全部会话，返回被吊销的会话数。"""
    if len(new) < 8:
        raise AuthError("新密码至少 8 位")
    user = users.verify(username, old)
    if not user:
        raise AuthError("原密码不正确")
    users.set_password(username, new)
    return rstore.delete_user_sessions(username)
