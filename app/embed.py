"""向量化门面。

真正的传输在 providers.py（本地 Ollama / 任意 OpenAI 兼容端点，可在设置里切换），
这里只负责把错误统一成 EmbedError，并暴露"当前生效的向量模型标识"。

魔改点：想让不同文档走不同的 embedding，改 embed_texts 的 provider/model 参数即可；
    但注意 chunks.embed_model 是按"当前设置"整体写入的，混用需自行调整。
"""

from __future__ import annotations

from app import providers, settings


class EmbedError(RuntimeError):
    pass


def current_model() -> str:
    """形如 "local:bge-m3"，写进 chunks.embed_model 用于隔离不同向量空间。"""
    return settings.current_embed_model()


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    try:
        return providers.embed(texts)
    except providers.ProviderError as exc:
        raise EmbedError(str(exc)) from exc


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
