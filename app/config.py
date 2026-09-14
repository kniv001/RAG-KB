"""统一配置 —— 魔改入口。

所有可调参数集中在这里，每一项都可用环境变量覆盖，不必改代码。
典型魔改点：
  · 换 embedding 模型   -> KB_EMBED_MODEL（注意同步改 KB_EMBED_DIM 并重建 chunks 表）
  · 换生成模型          -> KB_CHAT_MODEL
  · 调切分粒度          -> KB_CHUNK_SIZE / KB_CHUNK_OVERLAP
  · 调检索召回          -> KB_TOP_K / KB_MAX_DISTANCE
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"

# ---------- 向量化 ----------
EMBED_PROVIDER = os.environ.get("KB_EMBED_PROVIDER", "ollama")
EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("KB_EMBED_DIM", "1024"))
EMBED_BATCH = int(os.environ.get("KB_EMBED_BATCH", "16"))

# ---------- 生成 ----------
CHAT_MODEL = os.environ.get("KB_CHAT_MODEL", "qwen3:8b")
CHAT_TEMPERATURE = float(os.environ.get("KB_CHAT_TEMPERATURE", "0.2"))
CHAT_NUM_CTX = int(os.environ.get("KB_CHAT_NUM_CTX", "8192"))

# ---------- Ollama ----------
OLLAMA_BASE = os.environ.get("KB_OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = float(os.environ.get("KB_OLLAMA_TIMEOUT", "300"))

# ---------- 切分 ----------
CHUNK_SIZE = int(os.environ.get("KB_CHUNK_SIZE", "600"))       # 目标字符数
CHUNK_OVERLAP = int(os.environ.get("KB_CHUNK_OVERLAP", "80"))  # 相邻块重叠字符数
CHUNK_MIN = int(os.environ.get("KB_CHUNK_MIN", "80"))          # 小于此长度的块丢弃

# ---------- 检索 ----------
TOP_K = int(os.environ.get("KB_TOP_K", "6"))
MAX_DISTANCE = float(os.environ.get("KB_MAX_DISTANCE", "0.60"))  # 余弦距离上限，超出视为不相关

# ---------- 上限 ----------
MAX_UPLOAD_BYTES = int(os.environ.get("KB_MAX_UPLOAD_MB", "50")) * 1024 * 1024
ALLOWED_SUFFIX = {".txt", ".md", ".markdown", ".pdf", ".docx", ".csv", ".json", ".html"}
