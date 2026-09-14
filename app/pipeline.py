"""把各阶段串成两条链路。

索引链路：文件 -> parse -> chunk -> embed -> store
问答链路：问题 -> retrieve -> generate（无状态，不落库）

需要多轮对话与落库的走 app/chat.py 的 ask()。

魔改建议：想换掉某一环，改对应模块即可；想把整条链路换个玩法，
在这里改写 index_document / ask 两个函数，路由层不用动。
"""

from __future__ import annotations

from pathlib import Path

from app import chunk, config, embed, generate, parse, retrieve, store


class PipelineError(RuntimeError):
    pass


def index_document(doc_id: str) -> dict:
    """对一篇已上传的文档做解析 → 切分 → 向量化 → 入库。"""
    doc = store.get_document(doc_id)
    if not doc:
        raise PipelineError(f"文档不存在：{doc_id}")

    path = Path(config.ROOT) / doc["stored_path"]
    if not path.exists():
        raise PipelineError(f"文件缺失：{path}")

    try:
        text = parse.parse(path)
    except RuntimeError as exc:  # 缺可选依赖等
        raise PipelineError(str(exc)) from exc

    if not text.strip():
        raise PipelineError("解析结果为空，可能是扫描版 PDF 或空文件")

    chunks = chunk.split(text)
    if not chunks:
        raise PipelineError(f"切分后无有效分块（原文 {len(text)} 字）")

    try:
        vectors = embed.embed_texts(chunks)
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


def ask(
    question: str,
    provider: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    mode: str = retrieve.MODE_DEFAULT,
    with_answer: bool = True,
) -> dict:
    """无状态问答：检索（可选再生成），不写库。"""
    question = (question or "").strip()
    if not question:
        raise PipelineError("问题为空")

    hits = retrieve.search(question, top_k=top_k, mode=mode)

    result: dict = {
        "question": question,
        "sources": [
            {
                "doc_id": h["doc_id"],
                "doc_name": h["doc_name"],
                "seq": h["seq"],
                "score": h["score"],
                "distance": h["distance"],
                "preview": h["content"][:200],
            }
            for h in hits
        ],
    }

    if with_answer:
        if not hits:
            result["answer"] = "资料中没有相关内容。"
            result["provider"] = provider
            result["model"] = model
        else:
            try:
                g = generate.answer(question, hits, provider=provider, model=model)
            except generate.GenerateError as exc:
                raise PipelineError(f"生成失败：{exc}") from exc
            result["answer"] = g["content"]
            result["provider"] = g["provider"]
            result["model"] = g["model"]

    return result
