"""异步引擎 + 会话工厂 - 惰性初始化，DATABASE_URL 空时优雅降级

设计要点：
    - 全局单例引擎，首次访问时按 settings.database_url 创建
    - DATABASE_URL 空时 db_enabled() 返回 False，所有 DB 操作降级为 no-op
    - 连接池参数从 settings 读取（pool_size / max_overflow / pool_recycle）
    - SQLAlchemy 2.0 async 风格：create_async_engine + async_sessionmaker
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings

logger = logging.getLogger(__name__)

# 全局单例（惰性初始化，避免 import 时立即建连）
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def db_enabled() -> bool:
    """是否启用了主数据库。

    DATABASE_URL 非空且 sqlalchemy 已安装时返回 True。
    任何 DB 操作前都应先检查此函数，实现零侵入降级。
    """
    return bool(settings.database_url)


def _build_engine() -> AsyncEngine:
    """创建异步引擎（内部使用，不直接调用）。"""
    url = settings.database_url
    # asyncpg 驱动；若用户配置的是 postgresql:// 自动补全为 +asyncpg
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine_kwargs: dict[str, Any] = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle,
        "pool_pre_ping": True,  # 连接前探活，避免使用已被 DB 侧关闭的连接
    }
    # SQLite（测试用）不支持连接池参数
    if url.startswith("sqlite"):
        engine_kwargs = {}
    return create_async_engine(url, **engine_kwargs)


def get_engine() -> AsyncEngine:
    """获取全局异步引擎单例。

    Raises:
        RuntimeError: DATABASE_URL 未配置时调用。
    """
    global _engine
    if _engine is None:
        if not db_enabled():
            raise RuntimeError(
                "DATABASE_URL 未配置，主数据库未启用。"
                "请设置 DATABASE_URL 环境变量或使用文件存储降级路径。"
            )
        _engine = _build_engine()
        logger.info("主数据库异步引擎已创建: %s", _mask_url(settings.database_url))
    return _engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取全局异步会话工厂单例。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # commit 后对象仍可访问，避免 lazy load 触发额外查询
        )
    return _session_factory


async def init_db() -> None:
    """初始化数据库：建表（仅开发/测试用；生产走 Alembic 迁移）。

    生产环境应使用 `alembic upgrade head`，而非此函数。
    """
    if not db_enabled():
        logger.debug("DATABASE_URL 未配置，跳过 init_db")
        return
    from . import models  # noqa: F401 - 触发模型注册到 metadata
    from .base import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("主数据库表结构已创建（create_all）")


async def dispose_engine() -> None:
    """释放引擎资源（测试 / 优雅关闭时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("主数据库异步引擎已释放")


def _mask_url(url: str) -> str:
    """日志脱敏：隐藏 URL 中的密码。"""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"
