"""向量化门面 —— 带缓存。

流程：先按 (文本, 模型) 哈希查缓存 → 只对未命中的部分调 provider → 结果写回缓存。
向量化是整个索引链最慢的一步，缓存命中时重建索引几乎瞬间完成。

真正的传输在 providers.py（本地 Ollama / 任意 OpenAI 兼容端点，设置里可切）。
"""

from __future__ import annotations

from typing import Callable

from app import cache, config, providers, settings

Progress = Callable[[int, int, str], None]


class EmbedError(RuntimeError):
    pass


def current_model() -> str:
    """形如 "local:bge-m3"，写进 chunks.embed_model 用于隔离不同向量空间。"""
    return settings.current_embed_model()


def embed_texts(texts: list[str], on_progress: Progress | None = None) -> list[list[float]]:
    if not texts:
        return []

    model = current_model()
    hits, misses = cache.get_embeddings(texts, model)

    vectors: list[list[float] | None] = [None] * len(texts)
    for i, vec in hits.items():
        vectors[i] = vec

    if on_progress:
        on_progress(len(hits), len(texts), f"向量缓存命中 {len(hits)}/{len(texts)}")

    if misses:
        miss_texts = [texts[i] for i in misses]
        batch = max(1, config.EMBED_BATCH)
        done = len(hits)
        for start in range(0, len(miss_texts), batch):
            part = miss_texts[start : start + batch]
            try:
                part_vecs = providers.embed(part)
            except providers.ProviderError as exc:
                raise EmbedError(str(exc)) from exc
            cache.put_embeddings(part, part_vecs, model)
            for idx, vec in zip(misses[start : start + batch], part_vecs):
                vectors[idx] = vec
            done += len(part)
            if on_progress:
                on_progress(done, len(texts), f"向量化 {done}/{len(texts)}")

    return [v if v is not None else [] for v in vectors]


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


def check() -> dict:
    """自检：模型可用性 + 维度是否与建表时一致。"""
    try:
        vec = embed_query("维度自检")
    except EmbedError as exc:
        return {"ok": False, "error": str(exc)}
    got, want = len(vec), settings.load().get("embed_dim", 1024)
    return {
        "ok": got == want,
        "model": current_model(),
        "dim_actual": got,
        "dim_expected": want,
        "hint": None
        if got == want
        else "维度不一致：改 settings.json 的 embed_dim 后必须重建 chunks 表",
    }
