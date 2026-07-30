"""Sentry SDK 初始化与异常捕获工具（P1-2 企业级错误监控）

设计原则：
    - 零依赖降级：sentry-sdk 未安装时所有函数 no-op，不影响主流程
    - 配置驱动：DSN 留空时不初始化，零开销（开发/测试环境默认静默）
    - 集中入口：init_sentry() 在 FastAPI lifespan 启动时调用一次；
      capture_exception() / capture_message() 供中间件异常处理器调用
    - 自动 instrumentation：sentry-sdk[fastapi] 会自动注入 ASGI 中间件、
      SQL 查询、日志集成等，无需手动加中间件

集成点：
    - init_sentry()       —— app.py lifespan yield 前
    - capture_exception() —— middleware.py 兜底 Exception handler
    - add_request_tag()   —— RequestLoggingMiddleware 设置 request_id 关联
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# sentry-sdk 懒加载标志：首次 init_sentry() 时尝试 import
_sentry_available: bool | None = None


def _get_sentry() -> Any:
    """懒加载 sentry_sdk，返回模块对象或 None"""
    global _sentry_available
    if _sentry_available is False:
        return None
    try:
        import sentry_sdk  # type: ignore[import-not-found]
        from sentry_sdk.integrations.logging import LoggingIntegration  # type: ignore[import-not-found]
        _sentry_available = True
        return sentry_sdk
    except ImportError as exc:
        logger.info("sentry-sdk 未安装，错误监控降级为本地日志: %s", exc)
        _sentry_available = False
        return None


def init_sentry(
    dsn: str,
    environment: str = "production",
    traces_sample_rate: float = 0.1,
    release: str = "",
) -> bool:
    """初始化 Sentry SDK

    Args:
        dsn: Sentry 项目 DSN；留空时不初始化（零开销降级）
        environment: 环境名（production / staging / development）
        traces_sample_rate: 事务采样率 0.0~1.0
        release: 发布版本标签（建议 CI 注入语义版本或 git SHA）

    Returns:
        True=已初始化；False=未初始化（DSN 空 / sdk 未装）
    """
    if not dsn:
        logger.debug("SENTRY_DSN 未配置，跳过 Sentry 初始化")
        return False

    sentry_sdk = _get_sentry()
    if sentry_sdk is None:
        return False

    try:
        from sentry_sdk.integrations.logging import LoggingIntegration  # type: ignore[import-not-found]

        # 日志集成：WARNING 以上自动上报为 Sentry event，ERROR 以上附带完整栈
        logging_integration = LoggingIntegration(
            level=logging.INFO,        # INFO 及以上作为 breadcrumb
            event_level=logging.ERROR,  # ERROR 及以上作为 event
        )

        init_kwargs: dict[str, Any] = {
            "dsn": dsn,
            "environment": environment,
            "traces_sample_rate": traces_sample_rate,
            "integrations": [logging_integration],
            "send_default_pii": False,  # PIPL 合规：不自动采集 PII
            "attach_stacktrace": True,
            "max_breadcrumbs": 100,
        }
        if release:
            init_kwargs["release"] = release

        sentry_sdk.init(**init_kwargs)
        logger.info(
            "Sentry 初始化成功 env=%s traces_sample_rate=%s release=%s",
            environment, traces_sample_rate, release or "(auto)",
        )
        return True
    except Exception as exc:
        # Sentry 初始化失败绝不能阻塞应用启动
        logger.warning("Sentry 初始化失败，降级为本地日志: %s", exc)
        return False


def capture_exception(exc: Exception | None = None, **context: Any) -> None:
    """捕获异常上报到 Sentry（未初始化时 no-op）

    Args:
        exc: 异常对象；None 时捕获当前栈
        **context: 附加 tag/extra，会作为 Sentry tags/extras 上报
    """
    sentry_sdk = _get_sentry()
    if sentry_sdk is None:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                if isinstance(value, (str, int, float, bool)):
                    scope.set_tag(key, value)
                else:
                    scope.set_extra(key, value)
            if exc is not None:
                sentry_sdk.capture_exception(exc)
            else:
                sentry_sdk.capture_exception()
    except Exception as capture_exc:
        # 上报失败不能影响主流程
        logger.debug("Sentry capture_exception 失败: %s", capture_exc)


def capture_message(message: str, level: str = "info", **context: Any) -> None:
    """上报消息到 Sentry（未初始化时 no-op）

    Args:
        message: 消息文本
        level: info / warning / error / fatal
        **context: 附加 tag/extra
    """
    sentry_sdk = _get_sentry()
    if sentry_sdk is None:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                if isinstance(value, (str, int, float, bool)):
                    scope.set_tag(key, value)
                else:
                    scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level=level)
    except Exception as capture_exc:
        logger.debug("Sentry capture_message 失败: %s", capture_exc)


def add_request_tag(key: str, value: str) -> None:
    """为当前请求作用域设置 Sentry tag（供中间件调用关联 request_id）"""
    sentry_sdk = _get_sentry()
    if sentry_sdk is None:
        return
    try:
        sentry_sdk.set_tag(key, value)
    except Exception:
        pass


def is_initialized() -> bool:
    """Sentry SDK 是否已初始化（供测试断言）"""
    sentry_sdk = _get_sentry()
    if sentry_sdk is None:
        return False
    try:
        return sentry_sdk.is_initialized()
    except Exception:
        return False
