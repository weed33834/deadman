"""结构化日志配置 - structlog 与 stdlib logging 集成

设计目标
--------
1. 用 structlog 提供结构化（键值对）日志，同时与 stdlib ``logging`` 完全互通：
   现有 ``logging.getLogger(__name__)`` 调用无需修改，自动经 structlog 的
   ``ProcessorFormatter`` 统一渲染。
2. 通过环境变量控制输出格式与级别，便于在容器/生产环境切换 JSON 输出：
   - ``DEADMAN_LOG_FORMAT=json|console`` （默认 ``console``）
   - ``DEADMAN_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`` （默认 ``INFO``）
3. 提供 :func:`setup_logging`，在 CLI / Web / A2A 的入口函数中调用
   （**不要**在模块导入时调用，避免副作用污染测试）。

ProcessorPipeline
-----------------
``timestamper`` -> ``add_log_level`` -> ``JSONRenderer``（生产）/ ``ConsoleRenderer``（开发）

用法
----
入口点::

    from deadman.logging_config import setup_logging
    setup_logging()          # 读取环境变量
    setup_logging(level="DEBUG")  # 显式覆盖级别

新代码获取结构化 logger::

    import structlog
    log = structlog.get_logger(__name__).bind(agent_name="legal_advisor",
                                              session_id="abc123")
    log.info("agent invoked", turn_count=1)

向后兼容：现有 ``logging.getLogger(__name__)`` 调用会被 ``foreign_pre_chain``
捕获并经同一套 processor 渲染。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

try:
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover - structlog 是必需依赖，此处仅做优雅降级
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False


# =====================================================================
# 默认值
# =====================================================================
DEFAULT_LOG_FORMAT = "console"
DEFAULT_LOG_LEVEL = "INFO"

# 标记 handler 是否由本模块安装（用于幂等去重）
_STRUCTLOG_HANDLER_MARK = "_deadman_structlog_handler"

# 共享 processor 链：structlog 原生 logger 与 stdlib foreign logger 共用
# 顺序：合并 contextvars -> 补 level -> 补时间戳 -> 栈信息 -> 异常格式化
if _HAS_STRUCTLOG:
    _SHARED_PROCESSORS = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
else:  # pragma: no cover
    _SHARED_PROCESSORS = []


# =====================================================================
# 解析辅助
# =====================================================================
def _resolve_level(level: Optional[str]) -> int:
    """将字符串日志级别解析为 stdlib logging 的数字级别。

    ``level`` 为 ``None`` 时读取 ``DEADMAN_LOG_LEVEL`` 环境变量。
    """
    if level is None:
        level = os.environ.get("DEADMAN_LOG_LEVEL", DEFAULT_LOG_LEVEL)
    return getattr(logging, str(level).upper(), logging.INFO)


def _resolve_renderer(fmt: Optional[str]):
    """根据 ``DEADMAN_LOG_FORMAT`` 选择渲染器。

    ``json`` -> :class:`structlog.processors.JSONRenderer`（生产，机器可读）
    其他    -> :class:`structlog.dev.ConsoleRenderer`（开发，彩色人类可读）

    注意：``ConsoleRenderer`` 位于 ``structlog.dev``（非 ``structlog.processors``）。
    """
    if fmt is None:
        fmt = os.environ.get("DEADMAN_LOG_FORMAT", DEFAULT_LOG_FORMAT)
    if str(fmt).lower() == "json":
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()


# =====================================================================
# 公开 API
# =====================================================================
def setup_logging(level: Optional[str] = None, fmt: Optional[str] = None) -> None:
    """配置 structlog 与 stdlib logging 的集成。

    参数:
        level: 日志级别字符串（``DEBUG``/``INFO``/``WARNING``/``ERROR``）；
               ``None`` 则读 ``DEADMAN_LOG_LEVEL``（默认 ``INFO``）。
        fmt: 输出格式（``json``/``console``）；
             ``None`` 则读 ``DEADMAN_LOG_FORMAT``（默认 ``console``）。

    幂等：重复调用会移除由本模块安装的旧 handler 并重新配置，不会重复输出。
    不在模块导入时调用本函数——只在 CLI/Web/A2A 入口函数调用，避免副作用。
    """
    if not _HAS_STRUCTLOG:
        # 优雅降级：structlog 未安装时退回 stdlib basicConfig
        logging.basicConfig(
            level=_resolve_level(level),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            stream=sys.stderr,
        )
        return

    resolved_level = _resolve_level(level)
    renderer = _resolve_renderer(fmt)

    # 1) structlog 原生配置：structlog.get_logger() 产出的日志也走 stdlib
    #    最后一个 processor wrap_for_formatter 把事件交给 stdlib handler
    structlog.configure(
        processors=_SHARED_PROCESSORS
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 2) stdlib handler：用 ProcessorFormatter 统一渲染
    #    foreign_pre_chain 处理所有 stdlib 原生 logger.getLogger(__name__) 的记录
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, _STRUCTLOG_HANDLER_MARK, True)

    root_logger = logging.getLogger()
    # 幂等：仅移除本模块之前安装的 handler，保留第三方 handler
    for existing in list(root_logger.handlers):
        if getattr(existing, _STRUCTLOG_HANDLER_MARK, False):
            root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(resolved_level)


def get_logger(name: Optional[str] = None) -> Any:
    """获取一个结构化 logger。

    新代码推荐用 ``structlog.get_logger(__name__)`` 或本函数；
    现有 ``logging.getLogger(__name__)`` 仍完全兼容（经 ``foreign_pre_chain`` 处理）。
    """
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)
