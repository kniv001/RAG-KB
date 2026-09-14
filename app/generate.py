"""生成层：提示词 + 调用 providers.chat。

魔改点：
  · 改 SYSTEM 调整回答风格 / 引用格式 / 拒答边界
  · 改 build_messages 调整上下文如何拼接、历史保留几轮
  · 换模型不在这里 —— 去 data/settings.json 加 provider，运行时用参数选
"""

from __future__ import annotations

from app import providers, settings

SYSTEM = """你是一个严谨的个人知识库助手。

规则：
1. 只依据【参考资料】回答，不要编造资料中没有的内容。
2. 如果参考资料无法回答该问题，直接说明"资料中没有相关内容"，不要猜测。
3. 回答用中文，简洁准确；涉及要点时用条目列出。
4. 在引用了某段资料的地方，用 [编号] 标注来源。
5. 若用户是在追问上一轮内容，结合对话历史理解其指代，但事实依据仍须来自参考资料。"""

MAX_HISTORY_TURNS = 8


class GenerateError(RuntimeError):
    pass


def build_context(contexts: list[dict]) -> str:
    if not contexts:
        return "【参考资料】\n（无）"
    blocks = [
        f"[{i}] 来源：{c['doc_name']}（第 {c['seq']} 块）\n{c['content']}"
        for i, c in enumerate(contexts, 1)
    ]
    return "【参考资料】\n" + "\n\n".join(blocks)


def build_messages(
    question: str,
    contexts: list[dict],
    history: list[dict] | None = None,
) -> list[dict]:
    """history: [{role: 'user'|'assistant', content: str}, ...]，按时间正序。"""
    msgs: list[dict] = [{"role": "system", "content": SYSTEM}]

    for h in (history or [])[-MAX_HISTORY_TURNS * 2 :]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})

    msgs.append(
        {
            "role": "user",
            "content": f"{build_context(contexts)}\n\n【问题】\n{question}",
        }
    )
    return msgs


def answer(
    question: str,
    contexts: list[dict],
    history: list[dict] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    """返回 {content, provider, model}。"""
    messages = build_messages(question, contexts, history)
    try:
        return providers.chat(
            messages,
            provider=provider,
            model=model,
            temperature=float(settings.load().get("chat_temperature", 0.2)),
        )
    except providers.ProviderError as exc:
        raise GenerateError(str(exc)) from exc
