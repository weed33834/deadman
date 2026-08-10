"""G3 测试：MCP 客户端（连接外部 MCP Server + 工具注册）"""

from __future__ import annotations

import sys

from deadman.mcp_server.client import (
    McpClientConfig,
    McpClientManager,
    RemoteMcpConnection,
    _normalize_config,
    get_client_manager,
    load_client_configs,
    save_client_configs,
)
from deadman.mcp_server.server import mcp

_FAKE_SERVER_CODE = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    if method == "initialize":
        out = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake-ext", "version": "1.0"}}
    elif method == "tools/list":
        out = {"tools": [{
            "name": "fake_hello",
            "description": "外部示例工具",
            "inputSchema": {"type": "object", "properties": {"who": {"type": "string"}}},
        }]}
    elif method == "tools/call":
        params = req.get("params", {})
        out = {"ok": True, "tool": params.get("name"),
               "result": {"msg": "hi " + params.get("arguments", {}).get("who", "x")}}
    else:
        out = {}
    print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": out}), flush=True)
"""


def _fake_config(name: str = "fake-ext") -> McpClientConfig:
    return McpClientConfig(
        name=name, transport="stdio", command=sys.executable, args=["-c", _FAKE_SERVER_CODE]
    )


class TestConfigParsing:
    def test_normalize_valid(self):
        cfg = _normalize_config(
            {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "srv", "/tmp"]}
        )
        assert cfg is not None and cfg.name == "fs" and cfg.tool_prefix == "ext_fs_"

    def test_normalize_missing_name(self):
        assert _normalize_config({"transport": "stdio"}) is None

    def test_normalize_http_requires_url(self):
        assert _normalize_config({"name": "x", "transport": "http"}) is None

    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deadman.mcp_server.client._data_dir", lambda: tmp_path)
        cfg = McpClientConfig(name="demo", transport="http", url="http://127.0.0.1:9000/mcp")
        save_client_configs([cfg])
        loaded = load_client_configs()
        assert len(loaded) == 1 and loaded[0].url == "http://127.0.0.1:9000/mcp"


class TestRemoteConnection:
    async def test_connect_fetch_call(self):
        conn = RemoteMcpConnection(_fake_config())
        assert await conn.connect() is True
        tools = await conn.fetch_tools()
        assert len(tools) == 1 and tools[0]["name"] == "fake_hello"
        result = await conn.call_tool("fake_hello", {"who": "张三"})
        assert result["ok"] is True and result["result"]["result"]["msg"] == "hi 张三"
        await conn.close()
        assert not conn.connected

    async def test_connect_missing_command_fails(self):
        conn = RemoteMcpConnection(
            McpClientConfig(name="bad", transport="stdio", command="/no/such/binary")
        )
        assert await conn.connect() is False


class TestMcpClientManager:
    async def test_manager_add_remove_registers_tools(self):
        before = {t["name"] for t in mcp.list_tools()}
        mgr = get_client_manager()
        res = mgr.add_server(_fake_config("fake-ext").__dict__)
        assert res["ok"] is True and res.get("tool_count") == 1
        assert "ext_fake-ext_fake_hello" in {t["name"] for t in mcp.list_tools()}
        out = await mcp.call_tool("ext_fake-ext_fake_hello", {"who": "李四"})
        assert out["ok"] is True and out["result"]["result"]["msg"] == "hi 李四"
        mgr.remove_server("fake-ext")
        after = {t["name"] for t in mcp.list_tools()}
        assert "ext_fake-ext_fake_hello" not in after and before <= after

    def test_add_invalid_config(self):
        assert McpClientManager().add_server({"name": "", "transport": "stdio"})["ok"] is False
