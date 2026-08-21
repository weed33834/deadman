"""browser_automation 工具测试（不启动真实浏览器）

覆盖：
- feature flag 关闭 → ok=False
- action 白名单校验
- URL scheme 守卫（file:// 等阻断）
- playwright 未安装/未就绪时降级提示
- MCP 注册：工具名存在、权限等级 WRITE_ASYNC
"""

from __future__ import annotations

import pytest

import deadman.tools.browser as browser_mod
from deadman.tools.browser import _validate_url, run_browser_action


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """每个用例重置 playwright 探测缓存，避免跨用例污染"""
    browser_mod._probe_done = False
    yield
    browser_mod._probe_done = False


class TestGuards:
    async def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(browser_mod, "BROWSER_TOOL_ENABLED", False)
        r = await run_browser_action("navigate", url="https://example.com")
        assert r["ok"] is False
        assert "DEADMAN_BROWSER_TOOL_ENABLED" in r["error"]

    async def test_unknown_action_rejected(self, monkeypatch):
        monkeypatch.setattr(browser_mod, "BROWSER_TOOL_ENABLED", True)
        r = await run_browser_action("download_malware", url="https://example.com")
        assert r["ok"] is False
        assert "action" in r["error"]

    @pytest.mark.parametrize(
        ("bad_url", "why"),
        [
            ("file:///C:/Windows/win.ini", "scheme"),
            ("ftp://example.com/x", "scheme"),
            ("javascript:alert(1)", "scheme"),
            ("", "不能为空"),
            ("   ", "不能为空"),
            ("https://", "主机名"),
        ],
    )
    async def test_url_guard_blocks_non_http(self, monkeypatch, bad_url: str, why: str):
        monkeypatch.setattr(browser_mod, "BROWSER_TOOL_ENABLED", True)
        err = _validate_url(bad_url)
        assert err is not None and why in err, f"{bad_url!r} 应被阻断（{why}）"

    def test_valid_https_passes(self):
        assert _validate_url("https://example.com/page") is None
        assert _validate_url("http://example.com") is None


class TestDegradation:
    async def test_missing_playwright_returns_hint(self, monkeypatch):
        """playwright 探测失败 → ok=False + 安装提示（不抛异常）"""
        monkeypatch.setattr(browser_mod, "BROWSER_TOOL_ENABLED", True)

        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        r = await run_browser_action("navigate", url="https://example.com")
        assert r["ok"] is False
        assert "playwright" in r["error"] and "pip install" in r["error"]


class TestRegistration:
    def test_registered_in_mcp_server(self):
        from deadman.mcp_server.server import mcp

        names = {t["name"] for t in mcp.list_tools()}
        assert "browser_automation" in names

    def test_permission_level(self):
        from deadman.mcp_server.permissions import ToolPermission, get_permission

        assert get_permission("browser_automation") == ToolPermission.WRITE_ASYNC

    def test_registered_in_react_tools(self):
        from deadman.orchestration.react_loop import get_available_tools
        from deadman.orchestration.react_tools import register_default_react_tools

        register_default_react_tools()
        tools = get_available_tools()
        assert "browser" in tools
        assert "browser_automation" in tools
