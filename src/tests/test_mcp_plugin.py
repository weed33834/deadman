"""mcp_server 插件适配测试：协议合规 + 注册表登记 + 幂等性。"""

from __future__ import annotations

import pytest

from deadman.mcp_server import plugin
from deadman.plugins import Plugin, PluginRegistry


@pytest.fixture()
def registry() -> PluginRegistry:
    return PluginRegistry()


class TestMcpPlugin:
    def test_satisfies_protocol(self):
        assert isinstance(plugin, Plugin)
        assert plugin.meta.name == "deadman-mcp"
        assert plugin.meta.version == "6.0.0"

    def test_setup_registers_server_and_tools(self, registry: PluginRegistry):
        plugin.setup(registry)

        assert registry.has("deadman-mcp")
        servers = registry.lookup("mcp.servers")
        assert len(servers) == 1
        assert servers[0].name == "deadman-platform"
        # 工具清单已登记（server docstring 声明 15 个工具，此处只验证非空）
        tools = registry.lookup("mcp.tools")
        assert len(tools) >= 10
        assert all({"name", "description"} <= set(t) for t in tools)

    def test_setup_is_idempotent(self, registry: PluginRegistry):
        plugin.setup(registry)
        plugin.setup(registry)

        assert len(registry.lookup("mcp.servers")) == 1

    def test_teardown_rolls_back(self, registry: PluginRegistry):
        plugin.setup(registry)
        plugin.teardown(registry)

        assert not registry.has("deadman-mcp")
        assert registry.lookup("mcp.servers") == []
        assert registry.lookup("mcp.tools") == []
