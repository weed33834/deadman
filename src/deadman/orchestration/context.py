"""对话上下文 / token 预算管理（通用智能体基础件，原较薄）。

- ``estimate_tokens``：启发式估算 token（中文约 1 字/token，英文约 4 字符/token）。
- ``trim_history``：按 token 预算保留最近消息，超出截断（保留最新、丢最旧）。
- ``build_context``：构造发给 LLM 的消息序列，保证不超过预算。

配合记忆系统做「长对话上下文压缩」，避免 token 超限 / 成本失控。
"""

from __future__ import annotations

import re

__all__ = ["estimate_tokens", "trim_history", "build_context"]

_ASCII_RE = re.compile(r"[A-Za-z0-9]")


def estimate_tokens(text: str) -> int:
    """启发式 token 估算：中文字符按 1，ASCII 按 4 字符/token。"""
    if not text:
        return 0
    ascii_chars = len(_ASCII_RE.findall(text))
    cjk_chars = len(text) - ascii_chars
    return cjk_chars + max(1, ascii_chars // 4)


def trim_history(history: list[dict[str, str]], budget: int) -> list[dict[str, str]]:
    """按 token 预算保留最近消息（丢最旧）。历史空则返回空。

    Args:
        history: 消息列表 [{"role": ..., "content": ...}, ...]（时间正序）。
        budget: 允许的 token 总预算。
    """
    if not history:
        return []
    kept: list[dict[str, str]] = []
    total = 0
    # 从最新往前累计，满足预算；最后反转为时间正序
    for msg in reversed(history):
        cost = estimate_tokens(msg.get("content", "")) + 1  # +1 角色/结构开销
        if total + cost > budget and kept:
            break
        kept.append(msg)
        total += cost
    kept.reverse()
    return kept


def build_context(
    history: list[dict[str, str]],
    user_input: str,
    budget: int,
    system: str | None = None,
) -> list[dict[str, str]]:
    """构造发给 LLM 的消息序列（system + 裁剪后的历史 + 用户输入），控制在预算内。

    Returns:
        messages: 依次为 system（若有）、历史（裁剪）、user_input。
    """
    # 为 user_input 预留空间
    input_cost = estimate_tokens(user_input)
    history_budget = max(1, budget - input_cost - (estimate_tokens(system) if system else 0))
    trimmed = trim_history(history, history_budget)
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(trimmed)
    messages.append({"role": "user", "content": user_input})
    return messages
