"""文本切分：纯文本 -> 分块列表。

策略（可整体替换，只要保持 (text: str) -> list[str] 的签名）：
  1. 先按 Markdown 标题 / 空行切成段落，保住语义边界；
  2. 再把段落贪心打包到 CHUNK_SIZE 字符，超长段落硬切；
  3. 相邻块保留 CHUNK_OVERLAP 字符重叠，避免答案被切断在边界。
"""

from __future__ import annotations

import re

from app import config

_HEADING = re.compile(r"^#{1,6}\s+", re.M)


def _paragraphs(text: str) -> list[str]:
    """按空行分段；标题行强制独立成段。"""
    text = _HEADING.sub(lambda m: "\n\n" + m.group(0), text)
    parts = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in parts if p]


def _hard_split(block: str, size: int, overlap: int) -> list[str]:
    """超长段落按字符硬切。"""
    out: list[str] = []
    step = max(1, size - overlap)
    for start in range(0, len(block), step):
        piece = block[start : start + size].strip()
        if piece:
            out.append(piece)
        if start + size >= len(block):
            break
    return out


def split(text: str) -> list[str]:
    """切分为块，返回去掉空白块后的列表。"""
    size = config.CHUNK_SIZE
    overlap = min(config.CHUNK_OVERLAP, size // 2)

    chunks: list[str] = []
    buffer = ""

    for para in _paragraphs(text):
        if len(para) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_hard_split(para, size, overlap))
            continue

        if not buffer:
            buffer = para
        elif len(buffer) + len(para) + 2 <= size:
            buffer = f"{buffer}\n\n{para}"
        else:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap else ""
            buffer = f"{tail}\n\n{para}" if tail else para

    if buffer:
        chunks.append(buffer)

    return [c.strip() for c in chunks if len(c.strip()) >= config.CHUNK_MIN]
