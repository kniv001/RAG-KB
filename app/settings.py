"""运行时可改的设置 —— data/settings.json（含 API Key，已 gitignore）。

结构：
{
  "providers": {
    "<id>": {
      "kind": "ollama" | "openai",
      "label": "显示名",
      "base": "http://127.0.0.1:11434" 或 "https://api.deepseek.com/v1",
      "api_key": "sk-...",              // kind=openai 时使用
      "chat_models": ["..."],
      "embed_models": ["..."]
    }
  },
  "default_chat":  {"provider": "local", "model": "qwen3:8b"},
  "default_embed": {"provider": "local", "model": "bge-m3"},
  "embed_dim": 1024
}

魔改点：想接任何新服务（DeepSeek / 通义 / Moonshot / 智谱 / 硅基流动 / OpenAI /
本地 vLLM / LM Studio），只要是 OpenAI 兼容接口，加一条 kind=openai 的 provider 即可，
不用改代码。首次启动会自动写出这个文件，直接编辑或用 /api/settings 改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "data" / "settings.json"
EXAMPLE = ROOT / "settings.example.json"

DEFAULTS: dict[str, Any] = {
    "providers": {
        "local": {
            "kind": "ollama",
            "label": "本地 Ollama",
            "base": "http://127.0.0.1:11434",
            "api_key": "",
            "chat_models": ["qwen3:8b", "qwen3.5:9b", "llama3.1:8b"],
            "embed_models": ["bge-m3"],
        },
        "deepseek": {
            "kind": "openai",
            "label": "DeepSeek API",
            "base": "https://api.deepseek.com/v1",
            "api_key": "",
            "chat_models": ["deepseek-chat", "deepseek-reasoner"],
            "embed_models": [],
        },
    },
    "default_chat": {"provider": "local", "model": "qwen3:8b"},
    "default_embed": {"provider": "local", "model": "bge-m3"},
    "embed_dim": 1024,
}

_cache: dict[str, Any] | None = None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> dict[str, Any]:
    global _cache
    if _cache is not None and not force:
        return _cache
    if FILE.exists():
        try:
            user = json.loads(FILE.read_text("utf-8"))
        except json.JSONDecodeError:
            user = {}
        _cache = _deep_merge(DEFAULTS, user)
    else:
        _cache = json.loads(json.dumps(DEFAULTS))
        save(_cache)
    return _cache


def save(cfg: dict[str, Any]) -> None:
    global _cache
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    _cache = cfg


def resolve_chat(provider: str | None, model: str | None) -> tuple[str, str, dict]:
    """返回 (provider_id, model, provider_cfg)。未指定则取默认。"""
    cfg = load()
    pid = provider or cfg["default_chat"]["provider"]
    pcfg = cfg["providers"].get(pid)
    if not pcfg:
        raise KeyError(f"未配置的 provider：{pid}")
    return pid, (model or cfg["default_chat"]["model"]), pcfg


def resolve_embed(provider: str | None, model: str | None) -> tuple[str, str, dict]:
    cfg = load()
    pid = provider or cfg["default_embed"]["provider"]
    pcfg = cfg["providers"].get(pid)
    if not pcfg:
        raise KeyError(f"未配置的 provider：{pid}")
    return pid, (model or cfg["default_embed"]["model"]), pcfg


def public() -> dict[str, Any]:
    """给前端用的视图：API Key 只回传"是否已设置"，不回传明文。"""
    cfg = load()
    return {
        "providers": [
            {
                "id": pid,
                "kind": p["kind"],
                "label": p.get("label", pid),
                "base": p.get("base", ""),
                "has_key": bool(p.get("api_key")),
                "chat_models": p.get("chat_models", []),
                "embed_models": p.get("embed_models", []),
            }
            for pid, p in cfg["providers"].items()
        ],
        "default_chat": cfg["default_chat"],
        "default_embed": cfg["default_embed"],
        "embed_dim": cfg["embed_dim"],
    }


def current_embed_model() -> str:
    """当前生效的向量模型标识，写进 chunks.embed_model 用于隔离不同向量空间。"""
    cfg = load()
    d = cfg["default_embed"]
    return f"{d['provider']}:{d['model']}"
