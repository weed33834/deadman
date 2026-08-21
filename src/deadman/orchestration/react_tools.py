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


async def _wrap_digital_legacy(
    action: str = "summary",
    user_id: str = "default",
    asset_id: str | None = None,
    heir_id: str | None = None,
    category: str | None = None,
    name: str | None = None,
    access_hint: str | None = None,
    action_on_death: str | None = None,
    **_: Any,
) -> Any:
    """数字遗产清单工具：登记 / 查询 / 指派继承人 / 生成移交方案。

    action:
      - summary: 返回清单统计（资产数 / 未指派数 / 按类别分布）
      - add_asset: 新增一项数字资产（需 name + category）
      - assign: 把资产指派给继承人（需 asset_id + heir_id）
      - add_heir: 新增继承人（需 name）
      - plan: 生成 Markdown 移交 / 注销方案
    敏感字段 access_hint 由存储层加密，不落明文。
    """
    import os

    from ..digital_legacy import (
        AssetAction,
        DigitalAsset,
        DigitalLegacyStore,
        Heir,
        render_plan_markdown,
    )

    pw = os.environ.get("DEADMAN_LEGACY_PASSPHRASE", "").encode("utf-8")
    store = DigitalLegacyStore(user_id=user_id, passphrase=pw or None)

    if action == "summary":
        return {"ok": True, "summary": store.summary()}
    if action == "add_heir":
        if not name:
            return {"ok": False, "error": "name required"}
        reg = store.add_heir(Heir(id=heir_id or f"h_{os.urandom(4).hex()}", name=name))
        return {"ok": True, "heirs": [h.to_dict() for h in reg.heirs]}
    if action == "add_asset":
        if not name or not category:
            return {"ok": False, "error": "name & category required"}
        asset = DigitalAsset(
            id=asset_id or f"a_{os.urandom(4).hex()}",
            category=category,
            name=name,
            access_hint=access_hint or "",
            action_on_death=action_on_death or AssetAction.DECIDE.value,
        )
        reg = store.add_asset(asset)
        return {"ok": True, "assets": [a.to_dict() for a in reg.assets]}
    if action == "assign":
        if not asset_id or not heir_id:
            return {"ok": False, "error": "asset_id & heir_id required"}
        reg = store.assign_heir(asset_id, heir_id)
        a = next((x for x in reg.assets if x.id == asset_id), None)
        return {"ok": True, "assigned_heir_id": a.assigned_heir_id if a else None}
    if action == "plan":
        return {"ok": True, "plan": render_plan_markdown(store.load())}
    return {"ok": False, "error": f"unknown action: {action}"}


async def _wrap_web_search_official(query: str, max_results: int = 5, **_: Any) -> Any:
    """包装 web_search_official。"""
    from ..mcp_server import server as mcp_server

    fn = getattr(mcp_server, "web_search_official", None)
    if fn is None:
        return {"ok": False, "error": "web_search_official not available"}
    return await fn(query=query, max_results=max_results)


async def _wrap_deep_research(question: str = "", max_sources: int = 8, **_: Any) -> Any:
    """Deep Research 深度研究：迭代检索多源 + 交叉验证 + 带引用报告。"""
    from ..research.deep_research import deep_research

    if not question or not question.strip():
        return {"ok": False, "error": "question required"}
    report = await deep_research(question.strip(), max_sources=max_sources)
    return {"ok": True, **report.to_dict()}


async def _wrap_data_analysis(data: Any = None, question: str = "", **_: Any) -> Any:
    """数据分析：对表格数据（dict 列表）做描述性统计。"""
    from ..research.data_analysis import analyze

    if not isinstance(data, list):
        return {"ok": False, "error": "data 需为对象数组（表格行）"}
    return analyze(data, question=question)


async def _wrap_supervisor(question: str = "", **_: Any) -> Any:
    """Supervisor 层级编排：拆解复杂请求→委派子智能体→聚合。"""
    from ..orchestration.supervisor import supervise

    if not question or not question.strip():
        return {"ok": False, "error": "question required"}
    result = await supervise(question.strip())
    return {"ok": True, **result.to_dict()}


async def _wrap_browser(action: str = "get_text", url: str = "", selector: str = "", text: str = "", **_: Any) -> Any:
    """浏览器自动化：navigate/get_text/screenshot/click/fill（Playwright 驱动）。"""
    from ..tools.browser import run_browser_action

    return await run_browser_action(action=action, url=url, selector=selector, text=text)


# 注册表:工具名 → wrapper
_TOOL_WRAPPERS: dict[str, Callable[..., Awaitable[Any]]] = {
    "web_search": _wrap_web_search,
    "read_file": _wrap_read_file,
    "query_knowledge": _wrap_query_knowledge,
    "knowledge_lookup": _wrap_query_knowledge,  # 别名
    "knowledge_query": _wrap_query_knowledge,  # 别名
    "web_search_official": _wrap_web_search_official,
    "digital_legacy": _wrap_digital_legacy,
    "deep_research": _wrap_deep_research,
    "data_analysis": _wrap_data_analysis,
    "supervisor": _wrap_supervisor,
}


async def _wrap_awareness(text: str = "", **_: Any) -> Any:
    """思维意识识别：识别用户意图与安全状态，返回推荐能力路由。"""
    from ..awareness import assess

    if not text:
        return {"ok": False, "error": "text required"}
    result = await assess(text)
    return {"ok": True, **result.to_dict()}


# awareness 工具在 _wrap_awareness 定义后再注册（避免导入期未定义）
_TOOL_WRAPPERS["awareness"] = _wrap_awareness
_TOOL_WRAPPERS["browser"] = _wrap_browser
_TOOL_WRAPPERS["browser_automation"] = _wrap_browser  # 别名，对齐 MCP 工具名


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
