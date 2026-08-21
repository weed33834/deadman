"""G3 MCP 客户端管理路由 —— /api/mcp/*

把"连接外部 MCP Server"能力暴露给管理台。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...mcp_server.client import get_client_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp-client"])


@router.get("/servers")
async def mcp_list_servers() -> dict[str, Any]:
    return {"servers": get_client_manager().list_servers()}


@router.post("/servers")
async def mcp_add_server(
    config: dict[str, Any] = Body(default=None, description="外部 MCP Server 配置"),  # noqa: B008
) -> dict[str, Any]:
    config = config or {}
    result = get_client_manager().add_server(config)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "配置/连接失败"))
    return result


@router.post("/servers/{name}/connect")
async def mcp_connect_server(name: str) -> dict[str, Any]:
    result = get_client_manager().connect_server(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "连接失败"))
    return result


@router.post("/servers/{name}/disconnect")
async def mcp_disconnect_server(name: str) -> dict[str, Any]:
    return get_client_manager().disconnect_server(name)


@router.delete("/servers/{name}")
async def mcp_remove_server(name: str) -> dict[str, Any]:
    return get_client_manager().remove_server(name)


@router.post("/connect-all")
async def mcp_connect_all() -> dict[str, Any]:
    return get_client_manager().connect_all()
