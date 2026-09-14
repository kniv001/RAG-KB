"""把各阶段串成两条链路。

索引链路：文件 -> [解析缓存] parse -> chunk -> [向量缓存] embed -> store
问答链路：问题 -> retrieve -> [回答缓存] generate

耗时操作通过 report(done, total, message) 回调上报进度，供后台任务写库、前端轮询。

魔改建议：想换掉某一环，改对应模块即可；想把整条链路换个玩法，
在这里改写 index_document / answer_with_cache，路由层不用动。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from app import cache, chunk, config, embed, generate, parse, retrieve, settings, store

Progress = Callable[[int, int, str], None]


class PipelineError(RuntimeError):
    pass


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def index_document(doc_id: str, report: Progress | None = None) -> dict:
    """解析 → 切分 → 向量化 → 入库。可安全重复调用（整篇替换）。"""

    def say(done: int, total: int, msg: str) -> None:
        if report:
            report(done, total, msg)

    doc = store.get_document(doc_id)
    if not doc:
        raise PipelineError(f"文档不存在：{doc_id}")

    path = Path(config.ROOT) / doc["stored_path"]
    if not path.exists():
        raise PipelineError(f"文件缺失：{path}")

    say(0, 0, "计算文件指纹…")
    digest = file_hash(path)

    text = cache.get_parse(digest)
    if text is None:
        say(0, 0, "解析文档…")
        try:
            text = parse.parse(path)
        except RuntimeError as exc:  # 缺可选依赖等
            raise PipelineError(str(exc)) from exc
        if text.strip():
            cache.put_parse(digest, doc["name"], text)
    else:
        say(0, 0, "解析缓存命中")

    if not text.strip():
        raise PipelineError("解析结果为空，可能是扫描版 PDF 或空文件")

    chunks = chunk.split(text)
    if not chunks:
        raise PipelineError(f"切分后无有效分块（原文 {len(text)} 字）")
    say(0, len(chunks), f"切分为 {len(chunks)} 块")

    try:
        vectors = embed.embed_texts(chunks, on_progress=say)
    except embed.EmbedError as exc:
        raise PipelineError(f"向量化失败：{exc}") from exc

    n = store.replace_chunks(doc_id, chunks, vectors)

    return {
        "doc_id": doc_id,
        "name": doc["name"],
        "chars": len(text),
        "chunks": n,
        "embed_model": embed.current_model(),
        "dim": len(vectors[0]) if vectors else 0,
    }


def _sources(hits: list[dict]) -> list[dict]:
    return [
        {
            "doc_id": h["doc_id"],
            "doc_name": h["doc_name"],
            "seq": h["seq"],
            "score": h["score"],
            "distance": h["distance"],
            "preview": h["content"][:200],
        }
        for h in hits
    ]


def answer_with_cache(
    question: str,
    hits: list[dict],
    history: list[dict] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    """带缓存的生成。缓存键含「上下文哈希」与「历史哈希」——
    资料变了或对话历史变了，键就变，绝不会返回基于旧内容的陈旧答案。

    返回 {content, provider, model, sources, cached}。
    """
    cfg = settings.load()
    pid, mid, _ = settings.resolve_chat(provider, model)
    temperature = float(cfg.get("chat_temperature", 0.2))

    ctx_hash = cache.context_hash([h["content"] for h in hits])
    hist_hash = cache.history_hash(history)
    k = cache.answer_key(question, ctx_hash, hist_hash, pid, mid, temperature)

    hit = cache.get_answer(k)
    if hit:
        return {
            "content": hit["answer"],
            "provider": hit["provider"],
            "model": hit["model"],
            "sources": hit["sources"] or _sources(hits),
            "cached": True,
        }

    try:
        g = generate.answer(question, hits, history=history, provider=provider, model=model)
    except generate.GenerateError as exc:
        raise PipelineError(f"生成失败：{exc}") from exc

    sources = _sources(hits)
    cache.put_answer(k, question, g["content"], g["provider"], g["model"], sources)

    return {
        "content": g["content"],
        "provider": g["provider"],
        "model": g["model"],
        "sources": sources,
        "cached": False,
    }


def ask(
    question: str,
    provider: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    mode: str = retrieve.MODE_DEFAULT,
    with_answer: bool = True,
) -> dict:
    """无状态问答：检索（可选再生成），不写对话库。"""
    question = (question or "").strip()
    if not question:
        raise PipelineError("问题为空")

    hits = retrieve.search(question, top_k=top_k, mode=mode)

    result: dict = {"question": question, "sources": _sources(hits)}

    if with_answer:
        if not hits:
            result.update(answer="资料中没有相关内容。", provider=provider, model=model, cached=False)
        else:
            g = answer_with_cache(question, hits, provider=provider, model=model)
            result.update(answer=g["content"], provider=g["provider"],
                          model=g["model"], cached=g["cached"])
    return result
