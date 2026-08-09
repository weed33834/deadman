"""deadman 工具包 - 提供平台级工具实现

当前包含：
- web_search: Web 搜索工具（DuckDuckGo provider，httpx 直连）
  遵守 retrieval-guardrails（confidence 分类 + 低可信度标注）
  遵守 integrity-framework（失败返回空，不编造结果）
  遵守 input-guardrails（query 仅作为 URL params，不拼接 shell）
"""

from __future__ import annotations

from .web_search import (
    DuckDuckGoSearchProvider,
    SearchResult,
    WebSearchProvider,
    WebSearchTool,
)

__all__ = [
    "DuckDuckGoSearchProvider",
    "SearchResult",
    "WebSearchProvider",
    "WebSearchTool",
]
