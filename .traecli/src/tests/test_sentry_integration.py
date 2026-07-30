"""Sentry 集成测试（P1-2）

验证内容：
    - sentry-sdk 未安装时所有函数 no-op（零依赖降级）
    - DSN 留空时 init_sentry 返回 False，不初始化
    - capture_exception / capture_message / add_request_tag 未初始化时 no-op
    - 配置字段正确从环境变量读取

不验证真实 Sentry 上报（需网络 + DSN，属于集成测试范畴，标记 integration）。
"""

from __future__ import annotations

import importlib
import logging
from unittest.mock import MagicMock, patch

import pytest


# =====================================================================
# 1. 零依赖降级：sentry-sdk 未安装时所有函数 no-op
# =====================================================================


class TestSentryGracefulDegradation:
    """sentry-sdk 未安装 / DSN 未配置时的降级行为"""

    def test_get_sentry_returns_none_when_sdk_not_installed(self):
        """sdk 未安装时 _get_sentry 返回 None，且缓存降级结果"""
        from deadman.observability import sentry_init

        # 强制重置缓存，模拟首次调用
        original = sentry_init._sentry_available
        sentry_init._sentry_available = None
        try:
            with patch.dict("sys.modules", {"sentry_sdk": None}):
                # 触发 ImportError 路径
                result = sentry_init._get_sentry()
                # 第一次调用后应缓存为 False
                assert result is None
                assert sentry_init._sentry_available is False
        finally:
            sentry_init._sentry_available = original

    def test_init_sentry_returns_false_when_dsn_empty(self):
        """DSN 留空时 init_sentry 返回 False，不调用 sdk"""
        from deadman.observability.sentry_init import init_sentry

        assert init_sentry(dsn="") is False
        assert init_sentry(dsn="   ") is False

    def test_capture_exception_noop_when_uninitialized(self):
        """未初始化时 capture_exception 不抛异常（no-op）"""
        from deadman.observability.sentry_init import capture_exception

        # 不应抛出异常
        capture_exception(ValueError("test"), tag1="value1")
        capture_exception(None, tag2=123)

    def test_capture_message_noop_when_uninitialized(self):
        """未初始化时 capture_message 不抛异常（no-op）"""
        from deadman.observability.sentry_init import capture_message

        capture_message("test message", level="warning", tag="v")
        capture_message("another", level="error")

    def test_add_request_tag_noop_when_uninitialized(self):
        """未初始化时 add_request_tag 不抛异常（no-op）"""
        from deadman.observability.sentry_init import add_request_tag

        add_request_tag("request_id", "abc-123")
        add_request_tag("user_id", "u-456")

    def test_is_initialized_returns_false_when_sdk_missing(self):
        """sdk 未安装时 is_initialized 返回 False"""
        from deadman.observability import sentry_init

        original = sentry_init._sentry_available
        sentry_init._sentry_available = None
        try:
            with patch.dict("sys.modules", {"sentry_sdk": None}):
                assert sentry_init.is_initialized() is False
        finally:
            sentry_init._sentry_available = original


# =====================================================================
# 2. 初始化成功路径：mock sentry_sdk 验证调用参数
# =====================================================================


class TestSentryInitialization:
    """init_sentry 成功路径（mock sentry_sdk）"""

    def test_init_sentry_calls_sdk_with_correct_args(self):
        """DSN 非空 + sdk 已装 → 调用 sentry_sdk.init 传入正确参数"""
        from deadman.observability import sentry_init

        # 构造 mock sentry_sdk 模块
        mock_sdk = MagicMock()
        mock_logging_integration = MagicMock()
        mock_logging_module = MagicMock()
        mock_logging_module.LoggingIntegration = mock_logging_integration

        original_available = sentry_init._sentry_available
        sentry_init._sentry_available = None
        try:
            with patch.dict(
                "sys.modules",
                {
                    "sentry_sdk": mock_sdk,
                    "sentry_sdk.integrations.logging": mock_logging_module,
                },
            ):
                result = sentry_init.init_sentry(
                    dsn="https://test@sentry.example/1",
                    environment="staging",
                    traces_sample_rate=0.5,
                    release="v1.2.3",
                )
                assert result is True
                # 验证 sentry_sdk.init 被调用
                mock_sdk.init.assert_called_once()
                call_kwargs = mock_sdk.init.call_args.kwargs
                assert call_kwargs["dsn"] == "https://test@sentry.example/1"
                assert call_kwargs["environment"] == "staging"
                assert call_kwargs["traces_sample_rate"] == 0.5
                assert call_kwargs["release"] == "v1.2.3"
                assert call_kwargs["send_default_pii"] is False
                assert call_kwargs["attach_stacktrace"] is True
        finally:
            sentry_init._sentry_available = original_available

    def test_init_sentry_without_release_omits_release_kwarg(self):
        """release 留空时不传 release 参数（让 SDK 自动推断）"""
        from deadman.observability import sentry_init

        mock_sdk = MagicMock()
        mock_logging_module = MagicMock()
        mock_logging_module.LoggingIntegration = MagicMock()

        original = sentry_init._sentry_available
        sentry_init._sentry_available = None
        try:
            with patch.dict(
                "sys.modules",
                {
                    "sentry_sdk": mock_sdk,
                    "sentry_sdk.integrations.logging": mock_logging_module,
                },
            ):
                sentry_init.init_sentry(
                    dsn="https://test@sentry.example/1",
                    release="",
                )
                call_kwargs = mock_sdk.init.call_args.kwargs
                assert "release" not in call_kwargs
        finally:
            sentry_init._sentry_available = original

    def test_init_sentry_failure_does_not_raise(self):
        """sentry_sdk.init 抛异常时 init_sentry 捕获并返回 False，不传播"""
        from deadman.observability import sentry_init

        mock_sdk = MagicMock()
        mock_sdk.init.side_effect = RuntimeError("simulated init failure")
        mock_logging_module = MagicMock()
        mock_logging_module.LoggingIntegration = MagicMock()

        original = sentry_init._sentry_available
        sentry_init._sentry_available = None
        try:
            with patch.dict(
                "sys.modules",
                {
                    "sentry_sdk": mock_sdk,
                    "sentry_sdk.integrations.logging": mock_logging_module,
                },
            ):
                # 不应抛异常
                result = sentry_init.init_sentry(dsn="https://test@sentry.example/1")
                assert result is False
        finally:
            sentry_init._sentry_available = original


# =====================================================================
# 3. 配置字段读取
# =====================================================================


class TestSentryConfigFields:
    """Settings dataclass 中 Sentry 配置字段正确性"""

    def test_settings_has_sentry_fields(self):
        from deadman.config import Settings

        s = Settings()
        assert hasattr(s, "sentry_dsn")
        assert hasattr(s, "sentry_environment")
        assert hasattr(s, "sentry_traces_sample_rate")
        assert hasattr(s, "sentry_release")

    def test_settings_defaults(self, monkeypatch):
        """未设环境变量时使用合理默认值"""
        # 清除可能的环境变量
        for var in ("SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_TRACES_SAMPLE_RATE", "SENTRY_RELEASE"):
            monkeypatch.delenv(var, raising=False)

        from deadman.config import Settings
        s = Settings()
        assert s.sentry_dsn == ""
        assert s.sentry_environment == "production"
        assert s.sentry_traces_sample_rate == 0.1
        assert s.sentry_release == ""

    def test_settings_reads_env_vars(self, monkeypatch):
        """环境变量正确注入 Settings（重新加载模块以触发 dataclass 字段求值）

        注意：Settings dataclass 字段默认值用 os.getenv(...) 在类定义时求值，
        不是 __init__ 时，所以必须 reload 模块才能读到测试设置的环境变量。
        """
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/42")
        monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.25")
        monkeypatch.setenv("SENTRY_RELEASE", "v2.0.0-rc1")

        import deadman.config as config_module
        importlib.reload(config_module)
        try:
            s = config_module.Settings()
            assert s.sentry_dsn == "https://key@sentry.io/42"
            assert s.sentry_environment == "staging"
            assert s.sentry_traces_sample_rate == 0.25
            assert s.sentry_release == "v2.0.0-rc1"
        finally:
            # 恢复模块原始状态，避免污染后续测试
            monkeypatch.delenv("SENTRY_DSN", raising=False)
            monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
            monkeypatch.delenv("SENTRY_TRACES_SAMPLE_RATE", raising=False)
            monkeypatch.delenv("SENTRY_RELEASE", raising=False)
            importlib.reload(config_module)
