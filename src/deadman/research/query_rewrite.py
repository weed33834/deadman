"""查询改写（RAG 前处理基础件）：扩展 / 简化用户查询，提升检索召回。

- LLM 可用时：改写为「更利于检索」的查询（补全指代、扩展同义、聚焦实体）。
- LLM 不可用：退化返回原查询（零侵入，不抛异常）。

检索链路用法：``rewrite_query`` → ``gather_sources/org_doc_rag.query``。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _llm_available() -> bool:
    from ..llm import llm_client

    return bool(getattr(llm_client, "api_key", None))


async def rewrite_query(query: str, context: str = "") -> tuple[str, bool]:
    """改写查询以提升检索召回。返回 (改写后查询, 是否改写)。

    Args:
        query: 用户原始查询。
        context: 可选对话上下文（用于补全指代）。
    """
    if not query or not query.strip():
        return query, False
    q = query.strip()
    if not _llm_available():
        return q, False
    from ..llm import llm_client

    ctx_part = f"\n对话上下文：{context}" if context else ""
    try:
        out = await llm_client.chat(
            [
                {
                    "role": "system",
                    "content": "你是检索查询优化器。把用户查询改写为更利于"
                    "检索的查询：补全指代、扩展关键同义词、保留核心实体。只输出改写后的查询，不要解释。",
                },
                {"role": "user", "content": f"原始查询：{q}{ctx_part}"},
            ],
            temperature=0.2,
        )
        rewritten = (out or "").strip()
        if rewritten and rewritten != q:
            return rewritten, True
        return q, False
    except Exception as exc:  # pragma: no cover - 降级
        logger.warning("query_rewrite 失败，退化原查询: %s", exc)
        return q, False
