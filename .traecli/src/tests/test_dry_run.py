"""P3.2 测试矩阵 - 写操作 dry-run

覆盖：
  1. write_file dry-run 不实际写
  2. init_transfer dry-run 不触发
  3. execute_code dry-run 不执行
  4. dry_run_enabled=False 时 dry_run 参数被忽略（旧行为不变）

所有测试通过 monkeypatch 临时打开 DRY_RUN_ENABLED。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deadman.mcp_server import server as srv
from deadman.mcp_server.server import mcp


# =====================================================================
# 辅助 fixture
# =====================================================================


@pytest.fixture
def dry_run_enabled(monkeypatch):
    """临时打开 DRY_RUN_ENABLED"""
    monkeypatch.setattr(srv, "DRY_RUN_ENABLED", True)
    yield


@pytest.fixture
def dry_run_disabled(monkeypatch):
    """显式关闭 DRY_RUN_ENABLED（默认状态）"""
    monkeypatch.setattr(srv, "DRY_RUN_ENABLED", False)
    yield


# =====================================================================
# write_file dry-run
# =====================================================================


class TestWriteFileDryRun:
    async def test_write_file_dry_run_no_actual_write(
        self, dry_run_enabled, tmp_path, monkeypatch
    ):
        """dry_run=True 时只返回预览，不实际写文件"""
        # 让 settings.project_root 指向临时目录，确保文件不会真被写
        from deadman.config import Settings

        test_settings = Settings(project_root=tmp_path)
        monkeypatch.setattr(srv, "settings", test_settings)

        result = await mcp.call_tool(
            "write_file",
            {
                "path": "data/dry_run_test.txt",
                "content": "hello dry-run",
                "dry_run": True,
            },
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["would_write"].endswith("data/dry_run_test.txt")
        assert result["size"] == len("hello dry-run")
        # 文件不应实际被写
        assert not (tmp_path / "data" / "dry_run_test.txt").exists()

    async def test_write_file_dry_run_still_validates_path(
        self, dry_run_enabled, tmp_path, monkeypatch
    ):
        """dry_run=True 时仍应做路径校验（rules/ 应被拒）"""
        from deadman.config import Settings

        test_settings = Settings(project_root=tmp_path)
        monkeypatch.setattr(srv, "settings", test_settings)

        result = await mcp.call_tool(
            "write_file",
            {"path": "rules/integrity-framework.md", "content": "x", "dry_run": True},
        )
        assert result["ok"] is False
        assert "rules" in result["error"]


# =====================================================================
# init_transfer dry-run
# =====================================================================


class TestInitTransferDryRun:
    async def test_init_transfer_dry_run_no_trigger(self, dry_run_enabled):
        """dry_run=True 时只模拟转介，status=dry_run_preview"""
        result = await mcp.call_tool(
            "init_transfer",
            {
                "from_agent": "agent-a",
                "to_agent": "agent-b",
                "reason": "test",
                "current_question": "q",
                "context_summary": "ctx",
                "risk_tier": "R1",
                "urgency": "high",
                "dry_run": True,
            },
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["status"] == "dry_run_preview"
        assert result["would_trigger_transfer"] is True
        # dry-run 不需用户确认
        assert result["user_confirmation_required"] is False
        # 7 字段应完整
        assert result["fields_complete"] == 7

    async def test_init_transfer_dry_run_partial_fields(self, dry_run_enabled):
        """部分字段缺失时 fields_complete 应正确（urgency 默认 "normal" 算非空）"""
        result = await mcp.call_tool(
            "init_transfer",
            {
                "from_agent": "a",
                "to_agent": "b",
                "reason": "r",
                "current_question": "q",
                "dry_run": True,
            },
        )
        assert result["ok"] is True
        # from/to/reason/question/urgency("normal") 共 5 个非空
        assert result["fields_complete"] == 5


# =====================================================================
# execute_code dry-run
# =====================================================================


class TestExecuteCodeDryRun:
    async def test_execute_code_dry_run_no_execution(self, dry_run_enabled):
        """dry_run=True 时只校验语法，不实际执行"""
        result = await mcp.call_tool(
            "execute_code",
            {"code": "x = 1 + 2\nprint(x)", "dry_run": True},
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["would_execute"] is True
        assert result["syntax_ok"] is True
        assert result["backend"] == "dry_run"
        assert result["code_size"] > 0

    async def test_execute_code_dry_run_detects_syntax_error(
        self, dry_run_enabled
    ):
        """dry-run 应检测语法错误"""
        result = await mcp.call_tool(
            "execute_code",
            {"code": "def broken(:", "dry_run": True},
        )
        assert result["ok"] is False
        assert result["dry_run"] is True
        assert result["syntax_ok"] is False
        assert result["syntax_error"] is not None

    async def test_execute_code_dry_run_empty_code(self, dry_run_enabled):
        """空代码 dry-run 应返回 ok=False"""
        result = await mcp.call_tool(
            "execute_code",
            {"code": "", "dry_run": True},
        )
        assert result["ok"] is False
        assert "code 不能为空" in result["error"]


# =====================================================================
# dry_run 关闭时旧行为不变
# =====================================================================


class TestDryRunDisabled:
    async def test_dry_run_disabled_ignores_flag(
        self, dry_run_disabled, tmp_path, monkeypatch
    ):
        """DRY_RUN_ENABLED=False 时 dry_run=True 应被忽略，走真实路径"""
        from deadman.config import Settings

        test_settings = Settings(project_root=tmp_path)
        monkeypatch.setattr(srv, "settings", test_settings)

        result = await mcp.call_tool(
            "write_file",
            {
                "path": "data/real_write.txt",
                "content": "actual write",
                "dry_run": True,  # 应被忽略
                "overwrite": True,
                "create_dirs": True,
            },
        )
        # 走真实路径：应实际写文件
        assert result["ok"] is True
        assert "dry_run" not in result or result.get("dry_run") is not True
        assert (tmp_path / "data" / "real_write.txt").exists()
        assert (tmp_path / "data" / "real_write.txt").read_text() == "actual write"

    async def test_dry_run_disabled_init_transfer_normal(
        self, dry_run_disabled
    ):
        """DRY_RUN_ENABLED=False 时 init_transfer 应返回 pending_confirmation"""
        result = await mcp.call_tool(
            "init_transfer",
            {
                "from_agent": "a",
                "to_agent": "b",
                "reason": "r",
                "current_question": "q",
                "dry_run": True,  # 应被忽略
            },
        )
        # 应走真实路径
        assert result["status"] == "pending_confirmation"
        assert result["user_confirmation_required"] is True
        assert "dry_run" not in result
