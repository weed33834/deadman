"""MCP 客户端 —— 接入外部第三方 MCP Server

平台既作 MCP Server 对外提供工具，也可作 MCP 客户端接入外部第三方 MCP Server：
  * 连接外部 MCP Server，拉取其工具（tools/list）
  * 把外部工具以 ``ext_<server>_<tool>`` 前缀注册进本地 mcp 注册表 + ReAct 注册表，
    让智能体可直接调用
  * 提供连接管理（列出/连接/断开），并暴露给管理台

配置来源（按优先级）：
  1. 环境变量 ``DEADMAN_MCP_CLIENTS``（JSON 数组）
  2. 配置文件 ``~/.deadman/mcp_clients.json``（可通过管理台增删）

每条配置形如::

    {"name":"filesystem","transport":"stdio",
     "command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}
    {"name":"weather","transport":"http","url":"http://host:port/mcp"}

实现两条路径（沿用本仓库 try/except 降级风格）：
  1. 官方 ``mcp`` 客户端包（``mcp.ClientSession`` + ``mcp.client.stdio/sse``）：优先
  2. 纯 asyncio JSON-RPC 降级客户端：stdio（spawn 子进程行协议）/ http（POST JSON-RPC）

设计原则：
  * 全局专用事件循环，避免跨 loop 错误（asyncio 对象绑定创建它的循环）
  * 懒加载 / 幂等 / 单例；任何异常不抛出，返回结构化结果
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# 专用后台事件循环：MCP 客户端连接是 asyncio 对象，绑定到创建它的事件循环。
# 所有异步操作统一跑在同一个专用 loop（守护线程）上，避免跨 loop 错误。
# =====================================================================

_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None
_LOOP_LOCK = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
            _LOOP_THREAD = threading.Thread(
                target=_LOOP.run_forever, name="mcp-client-loop", daemon=True
            )
            _LOOP_THREAD.start()
        return _LOOP


def _run_async(coro: Any) -> Any:
    """把协程投递到全局专用事件循环执行并同步拿结果（同步/异步调用方皆可安全使用）。"""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# =====================================================================
# 可选依赖 —— 官方 mcp 客户端包（缺失则降级为纯 asyncio JSON-RPC）
# =====================================================================
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client

    _HAS_MCP_CLIENT = True
except Exception:  # pragma: no cover - 降级路径
    _HAS_MCP_CLIENT = False


def _data_dir() -> Path:
    d = Path.home() / ".deadman"
    d.mkdir(parents=True, exist_ok=True)
    return d


# =====================================================================
# 配置解析
# =====================================================================


@dataclass
class McpClientConfig:
    """一条外部 MCP Server 连接配置"""

    name: str
    transport: str = "stdio"  # stdio | http | sse
    command: str | None = None  # stdio: 启动命令
    args: list[str] = field(default_factory=list)
    url: str | None = None  # http/sse: 端点地址
    env: dict[str, str] = field(default_factory=dict)

    @property
    def tool_prefix(self) -> str:
        return f"ext_{self.name}_"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict.fromkeys(self.env, "***"),
            "tool_prefix": self.tool_prefix,
        }


def _normalize_config(raw: dict[str, Any]) -> McpClientConfig | None:
    name = str(raw.get("name", "")).strip()
    if not name:
        logger.warning("MCP client 配置缺少 name，跳过")
        return None
    safe = "".join(c for c in name if c.isalnum() or c in "_-.")
    if safe != name:
        logger.warning("MCP client 名称含不安全字符，仅保留 %r", safe)
        name = safe
    transport = str(raw.get("transport", "stdio")).lower()
    if transport not in ("stdio", "http", "sse"):
        logger.warning("MCP client %s transport=%s 不支持（用 stdio）", name, transport)
        transport = "stdio"
    cfg = McpClientConfig(
        name=name,
        transport=transport,
        command=str(raw.get("command") or "") or None,
        args=[str(a) for a in (raw.get("args") or [])],
        url=str(raw.get("url") or "") or None,
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
    )
    if cfg.transport == "stdio" and not cfg.command:
        logger.warning("MCP client %s: stdio 需提供 command，跳过", name)
        return None
    if cfg.transport in ("http", "sse") and not cfg.url:
        logger.warning("MCP client %s: %s 需提供 url，跳过", name, cfg.transport)
        return None
    return cfg


def load_client_configs() -> list[McpClientConfig]:
    merged: list[McpClientConfig] = []
    seen: set[str] = set()
    env_raw = os.getenv("DEADMAN_MCP_CLIENTS", "").strip()
    if env_raw:
        try:
            items = json.loads(env_raw)
            if isinstance(items, list):
                for item in items:
                    cfg = _normalize_config(item)
                    if cfg and cfg.name not in seen:
                        seen.add(cfg.name)
                        merged.append(cfg)
        except json.JSONDecodeError as exc:
            logger.warning("DEADMAN_MCP_CLIENTS 解析失败: %s", exc)
    cfg_file = _data_dir() / "mcp_clients.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("clients", [])
            for item in items:
                cfg = _normalize_config(item)
                if cfg and cfg.name not in seen:
                    seen.add(cfg.name)
                    merged.append(cfg)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("mcp_clients.json 读取失败: %s", exc)
    return merged


def save_client_configs(configs: list[McpClientConfig]) -> None:
    cfg_file = _data_dir() / "mcp_clients.json"
    payload = {
        "clients": [
            {
                "name": c.name,
                "transport": c.transport,
                "command": c.command,
                "args": list(c.args),
                "url": c.url,
                "env": c.env,
            }
            for c in configs
        ]
    }
    cfg_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# =====================================================================
# 连接实现
# =====================================================================


class RemoteMcpConnection:
    """对一个外部 MCP Server 的连接，提供统一 list_tools() / call_tool()。"""

    def __init__(self, cfg: McpClientConfig):
        self.cfg = cfg
        self.name = cfg.name
        self._session: Any = None
        self._cm_stack: list[Any] = []
        self._tools: dict[str, dict[str, Any]] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._stdin_w: Any = None
        self._read_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._session is not None or self._proc is not None

    async def _ensure_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({k: v for k, v in self.cfg.env.items() if v != "***"})
        # Windows 子进程默认 GBK 编码会弄坏中文参数/返回值，强制 UTF-8
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    async def _connect_official(self) -> bool:
        """官方 mcp client 连接：手动进入上下文管理器并保持打开。"""
        if not _HAS_MCP_CLIENT:
            return False
        try:
            if self.cfg.transport in ("stdio", "sse"):
                if self.cfg.transport == "stdio":
                    params = StdioServerParameters(
                        command=self.cfg.command or "",
                        args=list(self.cfg.args),
                        env=await self._ensure_env(),
                    )
                    cm = stdio_client(params)
                else:
                    cm = sse_client(self.cfg.url or "")
                # 提前入栈：任一步超时降级时 close() 也能完整清理
                self._cm_stack.append(cm)
                # 官方 stdio_client 在部分平台（Windows）initialize 可能永久挂起，
                # 必须加超时保护，超时后走自研降级客户端
                connect_timeout = 15.0
                read, write = await asyncio.wait_for(cm.__aenter__(), timeout=connect_timeout)
                session = ClientSession(read, write)
                await asyncio.wait_for(session.initialize(), timeout=connect_timeout)
                self._session = session
                return True
        except Exception as exc:
            logger.warning("MCP client %s 官方连接失败: %s", self.name, exc)
            await self.close()
            return False
        return False

    async def _connect_fallback_stdio(self) -> bool:
        try:
            env = await self._ensure_env()
            self._proc = await asyncio.create_subprocess_exec(
                self.cfg.command or "",
                *self.cfg.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            if self._proc.stdin is None or self._proc.stdout is None:
                raise RuntimeError("子进程 stdin/stdout 不可用")
            self._stdin_w = self._proc.stdin
            await self._rpc_fallback("initialize", {"protocolVersion": "2025-06-18"})
            return True
        except Exception as exc:
            logger.warning("MCP client %s stdio 降级连接失败: %s", self.name, exc)
            await self.close()
            return False

    async def _connect_fallback_http(self) -> bool:
        try:
            import httpx

            url = (self.cfg.url or "").rstrip("/") + "/mcp"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                resp.raise_for_status()
                if "error" in resp.json():
                    raise RuntimeError(resp.json()["error"])
            return True
        except Exception as exc:
            logger.warning("MCP client %s http 降级连接失败: %s", self.name, exc)
            return False

    async def _rpc_fallback(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """降级路径 JSON-RPC 请求（stdio 行协议 / http POST）。"""
        req_id = 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        if self.cfg.transport == "http":
            import httpx

            url = (self.cfg.url or "").rstrip("/") + "/mcp"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        else:
            if self._stdin_w is None:
                raise RuntimeError("stdio 通道未初始化")
            async with self._read_lock:
                line = json.dumps(payload, ensure_ascii=False)
                self._stdin_w.write((line + "\n").encode("utf-8"))
                await self._stdin_w.drain()
                if self._proc is None or self._proc.stdout is None:
                    raise RuntimeError("子进程 stdout 不可用")
                raw = await self._proc.stdout.readline()
                data = json.loads(raw.decode("utf-8", errors="replace").strip())
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("result", {})

    async def connect(self) -> bool:
        if self.connected:
            return True
        if self.cfg.transport == "stdio":
            if not await self._connect_official():
                await self.close()
                return await self._connect_fallback_stdio()
            return True
        if self.cfg.transport == "sse":
            if await self._connect_official():
                return True
            logger.warning("MCP client %s: sse 仅支持官方 mcp 客户端", self.name)
            return False
        if not await self._connect_official():
            await self.close()
            return await self._connect_fallback_http()
        return True

    async def fetch_tools(self) -> list[dict[str, Any]]:
        if not self.connected:
            if not await self.connect():
                return []
        try:
            if self._session is not None and hasattr(self._session, "list_tools"):
                listing = await self._session.list_tools()
                tools = [
                    {
                        "name": getattr(t, "name", ""),
                        "description": getattr(t, "description", ""),
                        "inputSchema": dict(getattr(t, "inputSchema", None) or {}),
                    }
                    for t in listing.tools
                ]
                self._tools = {t["name"]: t for t in tools}
                return tools
            result = await self._rpc_fallback("tools/list", {})
            tools = result.get("tools", [])
            self._tools = {t.get("name", ""): t for t in tools}
            return list(self._tools.values())
        except Exception as exc:
            logger.warning("MCP client %s fetch_tools 失败: %s", self.name, exc)
            return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.connected:
            if not await self.connect():
                return {"ok": False, "error": "not_connected", "message": "连接未建立"}
        try:
            if self._session is not None and hasattr(self._session, "call_tool"):
                result = await self._session.call_tool(name, arguments)
                content = getattr(result, "content", None)
                structured = getattr(result, "structuredContent", None)
                text = ""
                if isinstance(content, list):
                    for block in content:
                        if getattr(block, "type", "") == "text":
                            text += getattr(block, "text", "")
                elif isinstance(content, str):
                    text = content
                if structured:
                    return {"ok": True, "tool": name, "result": structured}
                return {"ok": True, "tool": name, "text": text}
            result = await self._rpc_fallback("tools/call", {"name": name, "arguments": arguments})
            return {"ok": True, "tool": name, "result": result}
        except Exception as exc:
            return {"ok": False, "tool": name, "error": type(exc).__name__, "message": str(exc)}

    async def close(self) -> None:
        try:
            if self._proc is not None:
                with contextlib.suppress(Exception):
                    self._proc.terminate()
                self._proc = None
                self._stdin_w = None
            for cm in reversed(self._cm_stack):
                with contextlib.suppress(Exception):
                    await cm.__aexit__(None, None, None)
            self._cm_stack = []
        except Exception:  # pragma: no cover
            pass
        self._session = None


# =====================================================================
# 管理器（单例）—— 连接 + 注册工具进本地注册表
# =====================================================================


class McpClientManager:
    def __init__(self) -> None:
        self._connections: dict[str, RemoteMcpConnection] = {}
        self._lock = threading.RLock()

    def _register_one_tool(self, conn: RemoteMcpConnection, tool: dict[str, Any]) -> dict[str, Any]:
        raw_name = tool.get("name", "")
        if not raw_name:
            return {"ok": False, "error": "external tool missing name"}
        ext_name = f"{conn.cfg.tool_prefix}{raw_name}"
        description = tool.get("description") or f"外部 MCP 工具 {conn.name}.{raw_name}"
        input_schema = tool.get("inputSchema") or {"type": "object", "properties": {}}

        async def _handler(**kwargs: Any) -> dict[str, Any]:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is _get_loop():
                return await conn.call_tool(raw_name, dict(kwargs))
            return _run_async(conn.call_tool(raw_name, dict(kwargs)))

        try:
            from ..mcp_server.server import mcp

            mcp.register_tool(
                name=ext_name,
                description=f"[外部 {conn.name}] {description}",
                input_schema=input_schema,
                handler=_handler,
            )
        except Exception as exc:
            logger.warning("MCP client %s 注册工具 %s 失败: %s", conn.name, ext_name, exc)
            return {"ok": False, "error": str(exc)}
        try:
            from ..orchestration.react_loop import register_react_tool

            register_react_tool(ext_name, _handler)
        except Exception as exc:
            logger.debug("ReAct 注册失败（不影响 mcp 注册）: %s", exc)
        return {"ok": True, "name": ext_name, "source": raw_name, "server": conn.name}

    def register_server_tools(self, conn: RemoteMcpConnection) -> dict[str, Any]:
        registered: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        if not conn.connected:
            try:
                _run_async(conn.connect())
            except Exception as exc:
                return {"ok": False, "server": conn.name, "error": str(exc)}
        tools = _run_async(conn.fetch_tools())
        for tool in tools:
            res = self._register_one_tool(conn, tool)
            (registered if res.get("ok") else failed).append(res)
        return {
            "ok": True,
            "server": conn.name,
            "transport": conn.cfg.transport,
            "tool_count": len(tools),
            "registered": registered,
            "failed": failed,
        }

    def connect_all(self) -> dict[str, Any]:
        with self._lock:
            results: dict[str, Any] = {}
            for cfg in load_client_configs():
                conn = self._connections.get(cfg.name)
                if conn is None:
                    conn = RemoteMcpConnection(cfg)
                    self._connections[cfg.name] = conn
                results[cfg.name] = (
                    {"ok": True, "connected": True}
                    if conn.connected
                    else self.register_server_tools(conn)
                )
            return {"ok": True, "servers": results}

    def list_servers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cfg in load_client_configs():
            conn = self._connections.get(cfg.name)
            out.append(
                {
                    "config": cfg.to_dict(),
                    "connected": conn.connected if conn else False,
                    "tool_count": len(conn._tools) if conn else 0,
                }
            )
        return out

    def connect_server(self, name: str) -> dict[str, Any]:
        with self._lock:
            cfg = next((c for c in load_client_configs() if c.name == name), None)
            if cfg is None:
                return {"ok": False, "error": f"未找到 MCP client 配置: {name}"}
            conn = self._connections.get(name)
            if conn is None:
                conn = RemoteMcpConnection(cfg)
                self._connections[name] = conn
            return self.register_server_tools(conn)

    def disconnect_server(self, name: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connections.pop(name, None)
            removed = 0
            try:
                from ..mcp_server.server import mcp

                for tname in list(mcp._tools.keys()):
                    if tname.startswith(f"ext_{name}_"):
                        mcp.unregister_tool(tname)
                        removed += 1
            except Exception as exc:
                logger.debug("移除 %s 工具失败: %s", name, exc)
            if conn is not None:
                _run_async(conn.close())
            return {"ok": True, "server": name, "tools_removed": removed}

    def add_server(self, raw: dict[str, Any]) -> dict[str, Any]:
        cfg = _normalize_config(raw)
        if cfg is None:
            return {"ok": False, "error": "配置不合法"}
        with self._lock:
            configs = load_client_configs()
            configs = [c for c in configs if c.name != cfg.name]
            configs.append(cfg)
            save_client_configs(configs)
        return self.connect_server(cfg.name)

    def remove_server(self, name: str) -> dict[str, Any]:
        with self._lock:
            self.disconnect_server(name)
            configs = load_client_configs()
            configs = [c for c in configs if c.name != name]
            save_client_configs(configs)
        return {"ok": True, "server": name}


_manager: McpClientManager | None = None
_manager_lock = threading.Lock()


def get_client_manager() -> McpClientManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = McpClientManager()
    return _manager


def connect_all_mcp_clients() -> dict[str, Any]:
    return get_client_manager().connect_all()
