"""用户表读写。密码只以 bcrypt 哈希形式存在于 users.password_hash。

首次启动时若 users 表为空且 data/auth.json 存在（旧版 HTTP Basic 的明文凭据），
会自动把那个账号迁移进来 —— 这样升级后你原来的密码仍然可用，但库里存的是哈希。
迁移完成后 data/auth.json 会被改名为 auth.json.migrated，不再被读取。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app import db, security

ROOT = Path(__file__).resolve().parent.parent
LEGACY_AUTH = ROOT / "data" / "auth.json"
USERNAME_FALLBACK = "kniv"


def get(username: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        return cur.fetchone()


def count() -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM users")
        return cur.fetchone()["n"]


def create(username: str, password: str) -> dict:
    uid = uuid.uuid4().hex[:12]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)"
            " RETURNING id, username, created_at",
            (uid, username, security.hash_password(password)),
        )
        return cur.fetchone()


def set_password(username: str, password: str) -> bool:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, updated_at = now() WHERE username = %s",
            (security.hash_password(password), username),
        )
        return cur.rowcount > 0


def touch_login(username: str) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET last_login_at = now() WHERE username = %s", (username,))


def verify(username: str, password: str) -> dict | None:
    """校验通过返回用户行，否则 None。用户名不存在时也走一次哈希计算，避免时序侧信道。"""
    user = get(username)
    if not user:
        security.verify_password(password, security.hash_password("dummy-timing-equalizer"))
        return None
    if not user.get("is_active", True):
        return None
    return user if security.verify_password(password, user["password_hash"]) else None


def migrate_legacy() -> dict | None:
    """把旧版 data/auth.json 的明文凭据迁移成 users 表里的哈希账号。只做一次。"""
    if count() > 0 or not LEGACY_AUTH.exists():
        return None
    try:
        data = json.loads(LEGACY_AUTH.read_text("utf-8"))
        username = data.get("user") or USERNAME_FALLBACK
        password = data.get("password") or ""
    except (json.JSONDecodeError, OSError):
        return None
    if not password:
        return None

    user = create(username, password)
    try:
        LEGACY_AUTH.rename(LEGACY_AUTH.with_suffix(".json.migrated"))
    except OSError:
        pass
    print(f"[users] migrated legacy basic-auth account '{username}' into users table", flush=True)
    return user
