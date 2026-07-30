"""ReAct 工具注册 - 把已有 MCP 工具的 async 函数注册到 ReAct 工具表

P0.4 配套:react_loop.py 通过 _TOOL_REGISTRY 分发 Action,本模块负责
把 mcp_server 中已实现的工具函数懒注册进去。

设计原则:
- 懒注册:避免 import 时副作用(MCP server 未初始化时也不报错)
- 容错:某个工具 import 失败不影响其他工具注册
- 不重复注册:已注册的工具跳过

注:mcp_server 中 @mcp.tool_auto 装饰的函数返回原函数(未包装),
因此可直接 await 调用,无需走 MCP 协议。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .react_loop import register_react_tool

logger = logging.getLogger(__name__)

# 标记是否已注册过(模块级单例,避免重复)
_TOOLS_REGISTERED = False


async def _wrap_web_search(query: str, max_results: int = 5, **_: Any) -> Any:
    """包装 web_search,适配 ReAct action_input。

    mcp_server.web_search 签名: (query: str, max_results: int = 5)
    ReAct action_input 可能多塞字段(如 tool / thought),用 **_ 吃掉。
    """
    from ..mcp_server import server as mcp_server

    return await mcp_server.web_search(query=query, max_results=max_results)


async def _wrap_read_file(
    path: str, encoding: str = "utf-8", max_bytes: int = 1048576, **_: Any
) -> Any:
    """包装 read_file。"""
    from ..mcp_server import server as mcp_server

    return await mcp_server.read_file(path=path, encoding=encoding, max_bytes=max_bytes)


async def _wrap_query_knowledge(
    country: str = "CN",
    topic: str = "",
    region: str | None = None,
    fallback_to_search: bool = True,
    query_mode: str = "vector",
    **_: Any,
) -> Any:
    """包装 query_knowledge(地域知识库查询)。

    支持宽松输入:LLM 可能只给 query 字符串,这里尝试拆分国家/主题。
    """
    from ..mcp_server import server as mcp_server

    fn = getattr(mcp_server, "query_knowledge", None)
    if fn is None:
        return {"ok": False, "error": "query_knowledge not available"}
    # 宽松输入:如果只传了 query,拆给 topic
    if not topic:
        topic = country  # 兜底:把第一个参数当主题
        if country == "CN":
            country = "CN"
    return await fn(
        country=country,
        topic=topic,
        region=region,
        fallback_to_search=fallback_to_search,
        query_mode=query_mode,  # type: ignore[arg-type]
    )


async def _wrap_web_search_official(query: str, max_results: int = 5, **_: Any) -> Any:
    """包装 web_search_official。"""
    from ..mcp_server import server as mcp_server

    fn = getattr(mcp_server, "web_search_official", None)
    if fn is None:
        return {"ok": False, "error": "web_search_official not available"}
    return await fn(query=query, max_results=max_results)


# 注册表:工具名 → wrapper
_TOOL_WRAPPERS: dict[str, Callable[..., Awaitable[Any]]] = {
    "web_search": _wrap_web_search,
    "read_file": _wrap_read_file,
    "query_knowledge": _wrap_query_knowledge,
    "knowledge_lookup": _wrap_query_knowledge,  # 别名
    "knowledge_query": _wrap_query_knowledge,  # 别名
    "web_search_official": _wrap_web_search_official,
}


def register_default_react_tools() -> None:
    """懒注册默认工具集。

    幂等:重复调用安全。失败的工具静默跳过(韧性优先)。
    供 agent_node 在启用 ReAct 时调用。
    """
    global _TOOLS_REGISTERED
    if _TOOLS_REGISTERED:
        return
    for name, wrapper in _TOOL_WRAPPERS.items():
        try:
            register_react_tool(name, wrapper)
        except Exception as e:  # pragma: no cover - 注册失败不阻断
            logger.warning("ReAct 工具 %s 注册失败: %s", name, e)
    _TOOLS_REGISTERED = True
