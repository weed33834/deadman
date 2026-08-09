"""FastAPI 依赖注入 - 异步会话

用法（web/deps.py 或路由）：
    from deadman.db.session import get_db_session

    @router.get("/items")
    async def list_items(session: AsyncSession = Depends(get_db_session)):
        ...

DATABASE_URL 未配置时返回 None，路由层应据此降级到文件存储。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from .engine import db_enabled, get_async_session_factory


async def get_db_session() -> AsyncIterator[AsyncSession | None]:
    """FastAPI 依赖：获取异步会话。

    DATABASE_URL 未配置时 yield None（降级信号），路由层据此走文件存储。
    """
    if not db_enabled():
        yield None
        return

    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
