"""密码哈希与令牌签发/校验。

密码：bcrypt（自带盐、自适工作因子）。数据库里只存哈希，永不存明文。
Access Token：JWT(HS256)。无状态，验签即可，每次请求零额外查询。
Refresh Token：不透明随机串，交给 rstore.py 存 Redis，可主动吊销。

密钥来源优先级：环境变量 KB_JWT_SECRET > data/jwt-secret.txt > 首次自动生成。
data/ 已 gitignore，密钥不会进仓库。
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

import bcrypt
import jwt

ROOT = Path(__file__).resolve().parent.parent
SECRET_FILE = ROOT / "data" / "jwt-secret.txt"

ALGORITHM = "HS256"
ISSUER = "rag-kb"

ACCESS_TTL = int(os.environ.get("KB_ACCESS_TTL", "1800"))       # 30 分钟
REFRESH_TTL = int(os.environ.get("KB_REFRESH_TTL", "2592000"))  # 30 天
BCRYPT_ROUNDS = int(os.environ.get("KB_BCRYPT_ROUNDS", "12"))


def _load_secret() -> str:
    if env := os.environ.get("KB_JWT_SECRET"):
        return env
    if SECRET_FILE.exists():
        s = SECRET_FILE.read_text("ascii").strip()
        if s:
            return s
    s = secrets.token_urlsafe(48)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_text(s, "ascii")
    print(f"[security] generated new JWT secret -> {SECRET_FILE}", flush=True)
    return s


SECRET = _load_secret()


# ---------------- 密码 ----------------

def hash_password(password: str) -> str:
    """bcrypt 哈希。注意 bcrypt 只取前 72 字节，超长密码先做不可逆处理。"""
    raw = _normalize(password)
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_normalize(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _normalize(password: str) -> bytes:
    """bcrypt 上限 72 字节，中文密码很容易超。先 sha256 再交给 bcrypt，
    这样任意长度都能安全使用，且不损失熵。"""
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).digest()


# ---------------- Access Token ----------------

def make_access_token(username: str) -> tuple[str, int]:
    """返回 (token, 有效期秒数)。"""
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + ACCESS_TTL,
        "iss": ISSUER,
        "typ": "access",
        "jti": secrets.token_urlsafe(8),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM), ACCESS_TTL


def decode_access_token(token: str) -> dict:
    """校验并解出载荷。失败抛 jwt 的异常，由调用方转成 401。"""
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM], issuer=ISSUER)


# ---------------- Refresh Token ----------------

def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)
