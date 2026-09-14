"""模型提供方传输层 —— 本地与云端走同一套接口，运行时可切换。

两种 kind：
  · ollama  —— 本地 Ollama 的 /api/chat 与 /api/embed
  · openai  —— 任何 OpenAI 兼容端点（DeepSeek / 通义 / Moonshot / 智谱 /
               硅基流动 / OpenAI / vLLM / LM Studio …），走 /chat/completions
              与 /embeddings，带 Bearer 鉴权

上层（generate.py / embed.py）只依赖 chat() 与 embed()，不关心具体是哪一家。
"""

from __future__ import annotations

from typing import Any

import httpx

from app import settings

TIMEOUT = 300.0


class ProviderError(RuntimeError):
    pass


def _headers(pcfg: dict) -> dict[str, str]:
    key = (pcfg.get("api_key") or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + path


# ---------------- 对话 ----------------

def chat(
    messages: list[dict],
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """返回 {content, provider, model, usage}。"""
    pid, mid, pcfg = settings.resolve_chat(provider, model)
    kind = pcfg.get("kind", "ollama")
    base = pcfg.get("base", "")
    if not base:
        raise ProviderError(f"provider {pid} 未配置 base 地址")

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            if kind == "ollama":
                r = client.post(
                    _join(base, "/api/chat"),
                    json={
                        "model": mid,
                        "messages": messages,
                        "stream": False,
                        "options": {"temperature": temperature},
                    },
                )
                r.raise_for_status()
                data = r.json()
                return {
                    "content": (data.get("message") or {}).get("content", "").strip(),
                    "provider": pid,
                    "model": mid,
                    "kind": kind,
                }

            if kind == "openai":
                r = client.post(
                    _join(base, "/chat/completions"),
                    headers=_headers(pcfg),
                    json={"model": mid, "messages": messages, "temperature": temperature},
                )
                r.raise_for_status()
                data = r.json()
                return {
                    "content": (data["choices"][0]["message"]["content"] or "").strip(),
                    "provider": pid,
                    "model": mid,
                    "kind": kind,
                    "usage": data.get("usage"),
                }

            raise ProviderError(f"未知的 provider kind：{kind}")
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300]
        raise ProviderError(
            f"[{pid}/{mid}] HTTP {exc.response.status_code}：{body}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"[{pid}/{mid}] 连接失败：{exc}") from exc


# ---------------- 向量 ----------------

def embed(
    texts: list[str],
    provider: str | None = None,
    model: str | None = None,
) -> list[list[float]]:
    if not texts:
        return []
    pid, mid, pcfg = settings.resolve_embed(provider, model)
    kind = pcfg.get("kind", "ollama")
    base = pcfg.get("base", "")

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            if kind == "ollama":
                r = client.post(_join(base, "/api/embed"), json={"model": mid, "input": texts})
                r.raise_for_status()
                return r.json()["embeddings"]

            if kind == "openai":
                r = client.post(
                    _join(base, "/embeddings"),
                    headers=_headers(pcfg),
                    json={"model": mid, "input": texts},
                )
                r.raise_for_status()
                return [d["embedding"] for d in r.json()["data"]]

            raise ProviderError(f"未知的 provider kind：{kind}")
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            f"[{pid}/{mid}] HTTP {exc.response.status_code}：{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"[{pid}/{mid}] 连接失败：{exc}") from exc


# ---------------- 探测 ----------------

def probe(provider: str | None = None) -> dict[str, Any]:
    """探测某个 provider 是否可用，并列出它当前真实可用的模型。"""
    cfg = settings.load()
    pid = provider or cfg["default_chat"]["provider"]
    pcfg = cfg["providers"].get(pid)
    if not pcfg:
        return {"ok": False, "error": f"未配置的 provider：{pid}"}

    kind, base = pcfg.get("kind", "ollama"), pcfg.get("base", "")
    try:
        with httpx.Client(timeout=15.0) as client:
            if kind == "ollama":
                models = [m["name"] for m in client.get(_join(base, "/api/tags")).json()["models"]]
            elif kind == "openai":
                r = client.get(_join(base, "/models"), headers=_headers(pcfg))
                r.raise_for_status()
                models = [m["id"] for m in r.json().get("data", [])]
            else:
                return {"ok": False, "error": f"未知 kind：{kind}"}
        return {"ok": True, "provider": pid, "kind": kind, "models": models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provider": pid, "error": f"{type(exc).__name__}: {exc}"}
