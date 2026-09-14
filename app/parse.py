"""文档解析：文件 -> 纯文本。

魔改点：新增格式只需在这里加一个分支，返回 str 即可。
pdf / docx 依赖为可选导入，缺库时给出明确报错而不是崩掉整个应用。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

_HTML_TAG = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)
_HTML_ANY = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub("\n\n", text)
    return text.strip()


def _from_html(raw: str) -> str:
    text = _HTML_TAG.sub(" ", raw)
    text = _HTML_ANY.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"[ \t]{2,}", " ", text)


def _from_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("读取 PDF 需要 pypdf：pip install pypdf") from exc
    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _from_docx(path: Path) -> str:
    try:
        import docx
    except ImportError as exc:
        raise RuntimeError("读取 DOCX 需要 python-docx：pip install python-docx") from exc
    document = docx.Document(str(path))
    return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())


def _from_csv(raw: str) -> str:
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return ""
    header, *body = rows
    lines = [" | ".join(header)]
    lines += [" | ".join(r) for r in body]
    return "\n".join(lines)


def _from_json(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return raw


def parse(path: Path) -> str:
    """把文件解析成纯文本。未知后缀按纯文本处理。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _clean(_from_pdf(path))
    if suffix == ".docx":
        return _clean(_from_docx(path))

    raw = path.read_text("utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        return _clean(_from_html(raw))
    if suffix == ".csv":
        return _clean(_from_csv(raw))
    if suffix == ".json":
        return _clean(_from_json(raw))
    return _clean(raw)
