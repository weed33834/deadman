"""MCP Server - 身后事多智能体平台的 Model Context Protocol 封装

提供 11 个工具供智能体调用，统一规则校验、知识查询、转介、自检等能力。
优先使用 FastMCP；若环境未安装 fastmcp，则降级为纯 Python async 实现。
"""

from __future__ import annotations

from .server import (
    McpServer,
    ToolDef,
    main,
    mcp,  # 全局 server 单例，已注册 11 个工具
)

__all__ = ["McpServer", "ToolDef", "main", "mcp"]
