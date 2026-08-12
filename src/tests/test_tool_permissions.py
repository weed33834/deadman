"""P3.3 测试矩阵 - 工具权限分级

覆盖：
  1. read-only 工具无需二次确认
  2. dangerous 工具必须二次确认
  3. 权限注册表覆盖全部 15 个工具
  4. 未知工具默认归 READ_ONLY（降级）

通过 monkeypatch 控制 feature flag。
"""

from __future__ import annotations

import pytest

from deadman.mcp_server import permissions as perm_module
from deadman.mcp_server.permissions import (
    PERMISSION_REGISTRY,
    ToolPermission,
    check_permission,
    get_permission,
    is_read_only,
    requires_confirmation,
)

# =====================================================================
# fixture
# =====================================================================


@pytest.fixture
def perms_enabled(monkeypatch):
    """临时打开 TOOL_PERMISSIONS_ENABLED"""
    monkeypatch.setattr(perm_module, "TOOL_PERMISSIONS_ENABLED", True)
    yield


@pytest.fixture
def perms_disabled(monkeypatch):
    """显式关闭 TOOL_PERMISSIONS_ENABLED（默认状态）"""
    monkeypatch.setattr(perm_module, "TOOL_PERMISSIONS_ENABLED", False)
    yield


# =====================================================================
# 权限注册表完整性
# =====================================================================


class TestPermissionRegistry:
    def test_permission_registry_complete(self):
        """注册表应覆盖全部 15 个工具"""
        # 15 个工具名（与 server.py 一致）
        expected = {
            "query_knowledge",
            "web_search",
            "web_search_official",
            "read_file",
            "write_file",
            "invoke_subagent",
            "check_integrity",
            "check_rules",
            "query_memory",
            "initiate_debate",
            "call_external_agent",
            "execute_reflexion",
            "init_transfer",
            "report_incident",
            "execute_code",
        }
        assert set(PERMISSION_REGISTRY.keys()) == expected

    def test_permission_registry_has_all_four_levels(self):
        """注册表应包含全部 4 个权限级别"""
        levels = set(PERMISSION_REGISTRY.values())
        assert ToolPermission.READ_ONLY in levels
        assert ToolPermission.WRITE_CONFIRM in levels
        assert ToolPermission.WRITE_ASYNC in levels
        assert ToolPermission.DANGEROUS in levels

    def test_dangerous_only_reflexion(self):
        """仅 execute_reflexion 归为 DANGEROUS"""
        dangerous = [
            name for name, perm in PERMISSION_REGISTRY.items() if perm == ToolPermission.DANGEROUS
        ]
        assert dangerous == ["execute_reflexion"]

    def test_write_confirm_includes_write_file_and_transfer(self):
        """write_file / init_transfer 归为 WRITE_CONFIRM"""
        assert PERMISSION_REGISTRY["write_file"] == ToolPermission.WRITE_CONFIRM
        assert PERMISSION_REGISTRY["init_transfer"] == ToolPermission.WRITE_CONFIRM

    def test_execute_code_is_write_async(self):
        """execute_code 归为 WRITE_ASYNC"""
        assert PERMISSION_REGISTRY["execute_code"] == ToolPermission.WRITE_ASYNC


# =====================================================================
# read-only 工具
# =====================================================================


class TestReadOnlyTools:
    def test_read_only_tools_no_confirmation(self, perms_enabled):
        """read-only 工具不应需要二次确认"""
        for tool in [
            "query_knowledge",
            "web_search",
            "web_search_official",
            "read_file",
            "check_integrity",
            "check_rules",
            "query_memory",
        ]:
            assert requires_confirmation(tool) is False, f"{tool} 不应需确认"
            assert is_read_only(tool) is True, f"{tool} 应为 read-only"

    def test_read_only_tools_cache_allowed(self, perms_enabled):
        """read-only 工具应允许缓存"""
        assert check_permission("query_knowledge", "cache") is True
        assert check_permission("read_file", "cache") is True

    def test_read_only_tool_call_allowed(self, perms_enabled):
        """read-only 工具调用应直接允许"""
        assert check_permission("query_knowledge", "call") is True


# =====================================================================
# dangerous 工具
# =====================================================================


class TestDangerousTools:
    def test_dangerous_tools_require_confirmation(self, perms_enabled):
        """dangerous 工具应需要二次确认"""
        assert requires_confirmation("execute_reflexion") is True

    def test_dangerous_tool_call_blocked_without_confirmation(self, perms_enabled):
        """dangerous 工具未确认时应被拒"""
        assert check_permission("execute_reflexion", "call") is False

    def test_dangerous_tool_call_confirmed_allowed(self, perms_enabled):
        """dangerous 工具显式 call_confirmed 时应允许"""
        assert check_permission("execute_reflexion", "call_confirmed") is True

    def test_dangerous_tool_not_cacheable(self, perms_enabled):
        """dangerous 工具不应被缓存"""
        assert check_permission("execute_reflexion", "cache") is False
        assert is_read_only("execute_reflexion") is False


# =====================================================================
# write 工具
# =====================================================================


class TestWriteTools:
    def test_write_confirm_tools_require_confirmation(self, perms_enabled):
        """write-confirm 工具应需要二次确认"""
        assert requires_confirmation("write_file") is True
        assert requires_confirmation("init_transfer") is True

    def test_write_async_tools_require_confirmation(self, perms_enabled):
        """write-async 工具应需要二次确认"""
        assert requires_confirmation("execute_code") is True

    def test_write_tools_not_cacheable(self, perms_enabled):
        """write 工具不应被缓存"""
        assert check_permission("write_file", "cache") is False
        assert check_permission("execute_code", "cache") is False


# =====================================================================
# 未知工具降级
# =====================================================================


class TestUnknownTool:
    def test_check_permission_unknown_tool(self, perms_enabled):
        """未知工具默认归 READ_ONLY（降级）"""
        assert get_permission("totally_unknown_tool") == ToolPermission.READ_ONLY
        # 未知工具应允许调用（不强制确认）
        assert requires_confirmation("totally_unknown_tool") is False
        assert check_permission("totally_unknown_tool", "call") is True
        # 未知工具应允许缓存（READ_ONLY）
        assert check_permission("totally_unknown_tool", "cache") is True


# =====================================================================
# feature flag 关闭时旧行为不变
# =====================================================================


class TestPermissionsDisabled:
    def test_disabled_no_confirmation(self, perms_disabled):
        """feature flag 关闭时 requires_confirmation 一律 False"""
        assert requires_confirmation("execute_reflexion") is False
        assert requires_confirmation("write_file") is False

    def test_disabled_all_calls_allowed(self, perms_disabled):
        """feature flag 关闭时 check_permission 一律 True"""
        assert check_permission("execute_reflexion", "call") is True
        assert check_permission("write_file", "cache") is True
        assert check_permission("any_tool", "any_action") is True

    def test_disabled_no_caching(self, perms_disabled):
        """feature flag 关闭时 is_read_only 一律 False（不缓存）"""
        assert is_read_only("query_knowledge") is False
        assert is_read_only("read_file") is False
