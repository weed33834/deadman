"""G1 测试：管理台相关运行时能力（工具启停 + 主 LLM 重配置）"""

from __future__ import annotations

from deadman import llm
from deadman.mcp_server import server as mcp_server_module
from deadman.mcp_server.server import mcp


class TestToolToggle:
    async def test_toggle_block_unblock(self):
        names = [t["name"] for t in mcp.list_tools()]
        assert names, "mcp 注册表不应为空"
        tool = names[0]
        try:
            mcp_server_module.set_tool_enabled(tool, False)
            assert mcp_server_module.is_tool_blocked(tool) is True
            assert mcp_server_module.list_tool_states()[tool] is False
            result = await mcp.call_tool(tool, {})
            assert result.get("ok") is False and result.get("error") == "tool_disabled"
        finally:
            mcp_server_module.set_tool_enabled(tool, True)
        assert mcp_server_module.is_tool_blocked(tool) is False

    def test_default_no_block(self):
        for t in mcp.list_tools():
            assert mcp_server_module.is_tool_blocked(t["name"]) is False


class TestReconfigureMainLLM:
    def test_reconfigure_and_restore(self):
        orig_provider, orig_model = llm.llm_client.provider, llm.llm_client.model
        try:
            r = llm.reconfigure_main_llm(provider="openai", model="gpt-5.4-mini")
            assert r["ok"] is True and llm.llm_client.model == "gpt-5.4-mini"
        finally:
            llm.reconfigure_main_llm(provider=orig_provider, model=orig_model)
        assert llm.llm_client.provider == orig_provider and llm.llm_client.model == orig_model
