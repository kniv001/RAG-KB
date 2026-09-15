"""Redis 支撑的会话存储 —— Refresh Token 与登录限速。

为什么 Refresh 放 Redis 而 Access 不放：
  Access 是 JWT，无状态、验签即可（零查询）；代价是签发后在有效期内无法撤回。
  所以给它短 TTL（30 分钟），把"能长期持有的那把钥匙"（Refresh）放 Redis —— 可以主动删除，
  于是"登出 / 改密码 / 踢下线"才真正生效。

键设计：
  rt:<token>              -> JSON{user, iat, ua}       TTL = REFRESH_TTL
  rt_user:<username>      -> SET(所有有效 token)      跟随 token 一起维护
  loginfail:<username>    -> 计数器                    TTL = 锁定窗口

兼容性：本机 Redis 是 3.0.504（2015 年版），redis-py 8.x 默认会发 RESP3 的 HELLO 握手
导致 `unknown command 'HELLO'`。必须显式 `protocol=2`。实测 PING/SET EX/TTL/管道/HASH/DEL 均正常。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import redis

from app import security

ROOT = Path(__file__).resolve().parent.parent
PASSWORD_FILE = ROOT / "data" / "redis-password.txt"

HOST = os.environ.get("KB_REDIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("KB_REDIS_PORT", "6379"))
DB = int(os.environ.get("KB_REDIS_DB", "0"))

MAX_LOGIN_FAILS = int(os.environ.get("KB_LOGIN_MAX_FAILS", "10"))
LOCK_WINDOW = int(os.environ.get("KB_LOGIN_LOCK_SECONDS", "900"))

_client: redis.Redis | None = None


def _password() -> str | None:
    if pw := os.environ.get("KB_REDIS_PASSWORD"):
        return pw
    return PASSWORD_FILE.read_text("ascii").strip() if PASSWORD_FILE.exists() else None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis(
            host=HOST,
            port=PORT,
            db=DB,
            password=_password(),
            decode_responses=True,
            protocol=2,          # ← 关键：兼容 Redis 3.0
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    return _client


class StoreError(RuntimeError):
    pass


def _rt_key(token: str) -> str:
    return f"rt:{token}"


def _user_key(username: str) -> str:
    return f"rt_user:{username}"


# ---------------- Refresh Token ----------------

def save_refresh(token: str, username: str, user_agent: str = "") -> None:
    r = client()
    payload = json.dumps(
        {"user": username, "iat": int(time.time()), "ua": user_agent[:120]},
        ensure_ascii=False,
    )
    try:
        pipe = r.pipeline()
        pipe.set(_rt_key(token), payload, ex=security.REFRESH_TTL)
        pipe.sadd(_user_key(username), token)
        pipe.expire(_user_key(username), security.REFRESH_TTL)
        pipe.execute()
    except redis.RedisError as exc:
        raise StoreError(f"Redis 写入失败：{exc}") from exc


def get_refresh(token: str) -> dict | None:
    try:
        raw = client().get(_rt_key(token))
    except redis.RedisError as exc:
        raise StoreError(f"Redis 读取失败：{exc}") from exc
    return json.loads(raw) if raw else None


def delete_refresh(token: str) -> None:
    r = client()
    data = get_refresh(token)
    try:
        pipe = r.pipeline()
        pipe.delete(_rt_key(token))
        if data and data.get("user"):
            pipe.srem(_user_key(data["user"]), token)
        pipe.execute()
    except redis.RedisError as exc:
        raise StoreError(f"Redis 删除失败：{exc}") from exc


def delete_user_sessions(username: str) -> int:
    """吊销某用户全部会话（改密码 / 踢下线时用）。"""
    r = client()
    try:
        tokens = r.smembers(_user_key(username)) or set()
        if tokens:
            r.delete(*[_rt_key(t) for t in tokens])
        r.delete(_user_key(username))
        return len(tokens)
    except redis.RedisError as exc:
        raise StoreError(f"Redis 批量删除失败：{exc}") from exc


def count_user_sessions(username: str) -> int:
    try:
        return int(client().scard(_user_key(username)) or 0)
    except redis.RedisError:
        return 0


# ---------------- 登录限速 ----------------

def login_locked(username: str) -> int:
    """返回剩余锁定秒数，0 表示未锁定。"""
    try:
        n = int(client().get(f"loginfail:{username}") or 0)
        if n < MAX_LOGIN_FAILS:
            return 0
        ttl = int(client().ttl(f"loginfail:{username}") or 0)
        return max(ttl, 0)
    except redis.RedisError:
        return 0


def note_login_fail(username: str) -> int:
    """记一次失败，返回累计次数。"""
    try:
        r = client()
        pipe = r.pipeline()
        pipe.incr(f"loginfail:{username}")
        pipe.expire(f"loginfail:{username}", LOCK_WINDOW)
        return int(pipe.execute()[0])
    except redis.RedisError:
        return 0


def clear_login_fail(username: str) -> None:
    try:
        client().delete(f"loginfail:{username}")
    except redis.RedisError:
        pass


# ---------------- 健康检查 ----------------

def health() -> dict:
    try:
        r = client()
        info = r.info("server")
        return {
            "ok": True,
            "version": info.get("redis_version"),
            "sessions": len(list(r.scan_iter("rt:*", count=200))),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
