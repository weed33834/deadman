"""P5.6 Honeypot 工具 - 测试矩阵

覆盖点：
1. test_is_honeypot_detects_fake_tools: 识别蜜罐工具
2. test_trigger_records_alert: 触发告警记录
3. test_register_honeypot_tools: 注册到 MCP server
4. test_honeypot_disabled_noop: feature flag 关闭行为不变
5. test_honeypot_handler_blocks: handler 始终阻断
6. test_honeypot_handler_triggers_alert: handler 触发告警
7. test_honeypot_global_singleton: 全局单例
8. test_honeypot_tool_definitions: 假工具定义完整
"""

from __future__ import annotations

import asyncio

import deadman.security.honeypot as honeypot_module
import pytest
from deadman.security.honeypot import (
    HONEYPOT_TOOL_DEFINITIONS,
    HONEYPOT_TOOLS,
    HoneypotManager,
    get_honeypot_manager,
    reset_honeypot_manager,
)

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_honeypot(monkeypatch):
    """每个测试默认开启 honeypot feature flag"""
    monkeypatch.setattr(honeypot_module, "HONEYPOT_ENABLED", True)
    reset_honeypot_manager()
    yield
    reset_honeypot_manager()


@pytest.fixture
def manager() -> HoneypotManager:
    """构造一个 HoneypotManager 实例"""
    return HoneypotManager()


class FakeMCPServer:
    """模拟 MCP server，记录注册的工具"""

    def __init__(self):
        self.registered_tools: dict[str, dict] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler,
        output_schema=None,
    ) -> None:
        self.registered_tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
            "output_schema": output_schema,
        }


# =====================================================================
# 1. is_honeypot 识别蜜罐工具
# =====================================================================


class TestIsHoneypotDetectsFakeTools:
    def test_is_honeypot_detects_fake_tools(self, manager):
        """HONEYPOT_TOOLS 中的工具名都识别为蜜罐"""
        for tool_name in HONEYPOT_TOOLS:
            assert manager.is_honeypot(tool_name) is True, (
                f"{tool_name} 应被识别为蜜罐"
            )

    def test_is_honeypot_rejects_real_tools(self, manager):
        """真实工具名不识别为蜜罐"""
        real_tools = [
            "web_search", "file_read", "write_file", "report_incident",
            "execute_code", "initiate_debate", "call_external_agent",
        ]
        for tool_name in real_tools:
            assert manager.is_honeypot(tool_name) is False, (
                f"{tool_name} 不应被识别为蜜罐"
            )

    def test_is_honeypot_empty_string(self, manager):
        """空字符串不是蜜罐"""
        assert manager.is_honeypot("") is False

    def test_honeypot_tools_non_empty(self):
        """HONEYPOT_TOOLS 至少 4 个假工具"""
        assert len(HONEYPOT_TOOLS) >= 4
        # 应包含计划文档中提到的 4 个
        assert "delete_all_files" in HONEYPOT_TOOLS
        assert "grant_admin_access" in HONEYPOT_TOOLS
        assert "disable_safety" in HONEYPOT_TOOLS
        assert "dump_credentials" in HONEYPOT_TOOLS


# =====================================================================
# 2. trigger 记录告警
# =====================================================================


class TestTriggerRecordsAlert:
    def test_trigger_records_alert(self, manager):
        """trigger 记录到内存 + 审计链"""
        manager.trigger("delete_all_files", caller="agent.evil")
        # 内存记录
        triggered = manager.get_triggered()
        assert len(triggered) == 1
        assert triggered[0]["tool_name"] == "delete_all_files"
        assert triggered[0]["caller"] == "agent.evil"

    def test_trigger_multiple_times(self, manager):
        """多次 trigger 累积记录"""
        manager.trigger("delete_all_files", "caller1")
        manager.trigger("dump_credentials", "caller2")
        manager.trigger("disable_safety", "caller3")
        assert len(manager.get_triggered()) == 3

    def test_trigger_empty_caller(self, manager):
        """caller 为空时不报错"""
        manager.trigger("delete_all_files")
        triggered = manager.get_triggered()
        assert len(triggered) == 1
        # caller 为空字符串

    def test_trigger_clear(self, manager):
        """clear_triggered 清空记录"""
        manager.trigger("delete_all_files", "caller")
        assert len(manager.get_triggered()) == 1
        manager.clear_triggered()
        assert len(manager.get_triggered()) == 0

    def test_trigger_writes_to_audit_chain(self, monkeypatch, tmp_path):
        """trigger 时若审计链启用，写入 security_alert 事件"""
        # 启用审计链
        import deadman.security.audit as audit_module
        monkeypatch.setattr(audit_module, "AUDIT_CHAIN_ENABLED", True)
        audit_module.reset_audit_chain()
        from deadman.security.audit import AuditChain
        audit_chain = AuditChain(persist_path=tmp_path / "audit.jsonl")

        # patch get_audit_chain 返回我们的临时 audit chain
        monkeypatch.setattr(
            "deadman.security.audit.get_audit_chain", lambda: audit_chain
        )

        mgr = HoneypotManager()
        mgr.trigger("delete_all_files", "agent.evil")

        # 审计链应有 1 条 security_alert 事件
        alerts = audit_chain.query(event_type="security_alert")
        assert len(alerts) == 1
        assert alerts[0].action == "honeypot_triggered"
        assert alerts[0].target == "delete_all_files"
        assert alerts[0].actor == "agent.evil"
        audit_module.reset_audit_chain()


# =====================================================================
# 3. register_honeypot_tools 注册到 MCP server
# =====================================================================


class TestRegisterHoneypotTools:
    def test_register_honeypot_tools(self, manager):
        """注册蜜罐工具到 MCP server"""
        fake_server = FakeMCPServer()
        count = manager.register_honeypot_tools(fake_server)
        # 应注册所有蜜罐工具
        assert count == len(HONEYPOT_TOOLS)
        assert len(fake_server.registered_tools) == len(HONEYPOT_TOOLS)
        # 每个工具都已注册
        for tool_name in HONEYPOT_TOOLS:
            assert tool_name in fake_server.registered_tools
            defn = fake_server.registered_tools[tool_name]
            # description 含 HONEYPOT 标记
            assert "HONEYPOT" in defn["description"]
            # input_schema 是 dict
            assert isinstance(defn["input_schema"], dict)
            # handler 是可调用的
            assert callable(defn["handler"])

    def test_register_returns_count(self, manager):
        """register_honeypot_tools 返回注册数量"""
        fake_server = FakeMCPServer()
        count = manager.register_honeypot_tools(fake_server)
        assert count == len(HONEYPOT_TOOLS)
        assert count > 0

    def test_register_with_none_server(self, manager):
        """mcp_server=None 时不报错，返回 0"""
        count = manager.register_honeypot_tools(None)
        assert count == 0

    def test_register_with_invalid_server(self, manager):
        """mcp_server 无 register_tool 方法时返回 0"""
        invalid_server = object()  # 无 register_tool 方法
        count = manager.register_honeypot_tools(invalid_server)
        assert count == 0


# =====================================================================
# 4. feature flag 关闭
# =====================================================================


class TestHoneypotDisabledNoop:
    def test_honeypot_disabled_is_honeypot_returns_false(self, monkeypatch):
        """feature flag 关闭：is_honeypot 始终返回 False"""
        monkeypatch.setattr(honeypot_module, "HONEYPOT_ENABLED", False)
        mgr = HoneypotManager()
        for tool_name in HONEYPOT_TOOLS:
            assert mgr.is_honeypot(tool_name) is False
        # 真实工具也是 False
        assert mgr.is_honeypot("web_search") is False

    def test_honeypot_disabled_trigger_noop(self, monkeypatch):
        """feature flag 关闭：trigger 不记录"""
        monkeypatch.setattr(honeypot_module, "HONEYPOT_ENABLED", False)
        mgr = HoneypotManager()
        mgr.trigger("delete_all_files", "caller")
        assert len(mgr.get_triggered()) == 0

    def test_honeypot_disabled_register_returns_zero(self, monkeypatch):
        """feature flag 关闭：register 返回 0，不注册"""
        monkeypatch.setattr(honeypot_module, "HONEYPOT_ENABLED", False)
        mgr = HoneypotManager()
        fake_server = FakeMCPServer()
        count = mgr.register_honeypot_tools(fake_server)
        assert count == 0
        assert len(fake_server.registered_tools) == 0


# =====================================================================
# 5. handler 始终阻断
# =====================================================================


class TestHoneypotHandlerBlocks:
    def test_honeypot_handler_blocks(self, manager):
        """handler 始终返回 blocked=True, ok=False"""
        fake_server = FakeMCPServer()
        manager.register_honeypot_tools(fake_server)

        # 调用 delete_all_files 的 handler
        handler = fake_server.registered_tools["delete_all_files"]["handler"]
        result = asyncio.run(handler(target="/", reason="test"))
        assert result["ok"] is False
        assert result["blocked"] is True
        assert "alert_id" in result
        assert "honeypot" in result["alert_id"]
        assert "蜜罐" in result["message"] or "blocked" in result["message"].lower()

    def test_honeypot_handler_triggers_alert(self, manager):
        """handler 调用时触发告警（记录到 manager.get_triggered）"""
        fake_server = FakeMCPServer()
        manager.register_honeypot_tools(fake_server)

        handler = fake_server.registered_tools["dump_credentials"]["handler"]
        asyncio.run(handler(caller="agent.attacker"))
        # 告警已记录
        triggered = manager.get_triggered()
        assert len(triggered) == 1
        assert triggered[0]["tool_name"] == "dump_credentials"
        assert triggered[0]["caller"] == "agent.attacker"

    def test_honeypot_handler_does_not_execute_dangerous_op(self, manager):
        """handler 不执行任何危险操作（仅返回阻断响应）"""
        fake_server = FakeMCPServer()
        manager.register_honeypot_tools(fake_server)

        handler = fake_server.registered_tools["delete_all_files"]["handler"]
        # 多次调用都安全阻断
        for _ in range(5):
            result = asyncio.run(handler(target="/etc", reason="malicious"))
            assert result["blocked"] is True
            assert result["ok"] is False


# =====================================================================
# 6. 全局单例
# =====================================================================


class TestHoneypotGlobalSingleton:
    def test_get_honeypot_manager_singleton(self):
        """get_honeypot_manager 返回同一实例"""
        m1 = get_honeypot_manager()
        m2 = get_honeypot_manager()
        assert m1 is m2

    def test_reset_honeypot_manager(self):
        """reset 后下次 get 返回新实例"""
        m1 = get_honeypot_manager()
        reset_honeypot_manager()
        m2 = get_honeypot_manager()
        assert m1 is not m2


# =====================================================================
# 7. 假工具定义完整
# =====================================================================


class TestHoneypotToolDefinitions:
    def test_honeypot_tool_definitions_complete(self):
        """每个假工具定义包含 name/description/input_schema"""
        assert len(HONEYPOT_TOOL_DEFINITIONS) == len(HONEYPOT_TOOLS)
        for defn in HONEYPOT_TOOL_DEFINITIONS:
            assert "name" in defn
            assert "description" in defn
            assert "input_schema" in defn
            assert defn["name"] in HONEYPOT_TOOLS
            assert "HONEYPOT" in defn["description"]

    def test_honeypot_definitions_unique_names(self):
        """假工具定义的 name 唯一"""
        names = [d["name"] for d in HONEYPOT_TOOL_DEFINITIONS]
        assert len(set(names)) == len(names)
