"""Repository 层 - DB↔文件双写抽象

设计：
    - BaseRepository: 提供 DB 会话获取 + 降级判断的通用基类
    - UserRepository: 用户 CRUD，DB 优先 / 文件回退的双写实现
    - CronJobRepository: Cron 任务（扩展④b 完善）
    - 双写策略：写操作 DB+文件同步（DB 主，文件备）；
      读操作 DB 优先，DB 未命中或未启用时回退文件存储。

与现有代码的关系：
    现有 auth/store.py UserStore 等保持不变（文件存储原样工作）。
    Repository 是上层封装：DB 启用时双写，未启用时纯转发给文件存储。
    这样实现零停机迁移：先部署代码（双写）→ 回填历史数据 → 切读 DB → 移除文件写。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .engine import db_enabled, get_async_session_factory
from .models import User

logger = logging.getLogger(__name__)


# =====================================================================
# BaseRepository
# =====================================================================
class BaseRepository:
    """Repository 基类 - 提供会话管理与降级判断"""

    @staticmethod
    def _db_active(session: AsyncSession | None) -> bool:
        """判断当前是否会话是否可用于 DB 操作。"""
        return session is not None and db_enabled()

    @staticmethod
    def _new_session() -> AsyncSession | None:
        """获取一个新的异步会话（无 FastAPI 依赖注入时使用）。

        DATABASE_URL 未配置时返回 None。
        调用方需自行 `async with` 管理生命周期。
        """
        if not db_enabled():
            return None
        return get_async_session_factory()()

    @staticmethod
    def _to_dict(obj: Any) -> dict[str, Any]:
        """将 ORM 对象转为 dict（不含 SQLAlchemy 内部字段）。"""
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# =====================================================================
# UserRepository - 用户表双写
# =====================================================================
class UserRepository(BaseRepository):
    """用户 Repository - DB↔文件双写

    用法：
        repo = UserRepository(file_store=UserStore())
        user = await repo.create(email, password, display_name)  # 自动双写

    DB 未启用时，所有操作纯转发给 file_store（向后兼容）。
    """

    def __init__(self, file_store: Any | None = None) -> None:
        """
        Args:
            file_store: 现有 UserStore 实例（文件存储）。None 时仅用 DB。
        """
        self.file_store = file_store

    # ------------------------------------------------------------------
    # 创建用户
    # ------------------------------------------------------------------
    async def create(
        self, email: str, password: str, display_name: str | None = None
    ) -> dict[str, Any]:
        """创建用户 - 双写（文件存储负责密码哈希，DB 同步元数据）。

        策略：先写文件存储（含密码哈希逻辑），成功后同步到 DB。
        DB 写失败仅记日志不阻断（文件存储是 source of truth during migration）。
        """
        # 1. 文件存储创建（含密码哈希、email_hmac、唯一性校验）
        if self.file_store is not None:
            result = self.file_store.register(email, password, display_name)
            user_id = result["user_id"]
        else:
            # 纯 DB 模式（无文件回退）- 需自行哈希
            result = await self._create_db_only(email, password, display_name)
            return result

        # 2. DB 同步（best-effort，失败不阻断文件存储结果）
        await self._sync_user_to_db(user_id, result)
        return result

    async def _sync_user_to_db(self, user_id: str, public_view: dict[str, Any]) -> None:
        """将文件存储的用户记录同步到 DB（best-effort）。"""
        if not db_enabled():
            return
        try:
            # 从文件存储读取完整记录（含 password_hash/salt）
            full_record = self.file_store.get_user_raw(user_id) if self.file_store else None
            if full_record is None:
                logger.warning("同步用户到 DB 失败：文件存储无完整记录 user=%s", user_id)
                return

            async with get_async_session_factory()() as session:
                existing = await session.get(User, user_id)
                if existing is not None:
                    return  # 已存在，跳过
                created_at = _parse_dt(full_record.get("created_at"))
                user = User(
                    user_id=user_id,
                    email=full_record.get("email", ""),
                    email_hmac=full_record.get("email_hmac", ""),
                    password_hash=full_record.get("password_hash", ""),
                    salt=full_record.get("salt", ""),
                    role=full_record.get("role", "user"),
                    family_id=full_record.get("family_id"),
                    display_name=full_record.get("display_name", ""),
                    password_updated_at=_parse_dt(full_record.get("password_updated_at")),
                    created_at=created_at or datetime.utcnow(),
                    updated_at=created_at or datetime.utcnow(),
                )
                session.add(user)
                await session.commit()
        except Exception as exc:
            logger.warning("同步用户到 DB 失败（best-effort，不阻断）user=%s: %s", user_id, exc)

    async def _create_db_only(
        self, email: str, password: str, display_name: str | None
    ) -> dict[str, Any]:
        """纯 DB 模式创建用户（无文件回退时）。"""
        import hashlib
        import hmac
        import os
        import uuid

        email_normalized = email.strip().lower()
        salt = os.urandom(16)
        # PBKDF2-HMAC-SHA256, 100k iterations（与 UserStore 一致）
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000, 32)
        server_secret = os.getenv("DEADMAN_JWT_SECRET", "deadman-default-secret").encode()
        email_hmac = hmac.new(server_secret, email_normalized.encode(), hashlib.sha256).hexdigest()
        user_id = str(uuid.uuid4())
        now = datetime.utcnow()

        async with get_async_session_factory()() as session:
            user = User(
                user_id=user_id,
                email=email_normalized,
                email_hmac=email_hmac,
                password_hash=password_hash.hex(),
                salt=salt.hex(),
                role="user",
                display_name=display_name or email_normalized.split("@")[0],
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.commit()
        return {
            "user_id": user_id,
            "email": email_normalized,
            "display_name": display_name or email_normalized.split("@")[0],
            "created_at": now.isoformat(),
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        """按 user_id 查询 - DB 优先，回退文件存储。"""
        if db_enabled():
            try:
                async with get_async_session_factory()() as session:
                    user = await session.get(User, user_id)
                    if user is not None:
                        return _user_to_public_view(user)
            except Exception as exc:
                logger.warning("DB 查询用户失败，回退文件存储 user=%s: %s", user_id, exc)
        # 回退文件存储
        if self.file_store is not None:
            return self.file_store.get_user(user_id)
        return None

    async def get_by_email_hmac(self, email_hmac: str) -> dict[str, Any] | None:
        """按 email_hmac 查询（登录验证用）- DB 优先（O(1) 索引）。"""
        if db_enabled():
            try:
                async with get_async_session_factory()() as session:
                    stmt = select(User).where(User.email_hmac == email_hmac)
                    user = (await session.execute(stmt)).scalar_one_or_none()
                    if user is not None:
                        return _user_to_public_view(user)
            except Exception as exc:
                logger.warning("DB 按 email_hmac 查询失败: %s", exc)
        return None

    async def count(self) -> int:
        """用户总数 - DB 优先。"""
        if db_enabled():
            try:
                from sqlalchemy import func

                async with get_async_session_factory()() as session:
                    stmt = select(func.count()).select_from(User)
                    return int((await session.execute(stmt)).scalar() or 0)
            except Exception as exc:
                logger.warning("DB 用户计数失败: %s", exc)
        if self.file_store is not None:
            return len(self.file_store._load())
        return 0


# =====================================================================
# 辅助函数
# =====================================================================
def _parse_dt(v: Any) -> datetime | None:
    """解析 ISO 格式 datetime 字符串（兼容文件存储格式）。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


def _user_to_public_view(user: User) -> dict[str, Any]:
    """ORM User → 公开视图 dict（不含 password_hash/salt）。"""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "family_id": user.family_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


__all__ = [
    "BaseRepository",
    "UserRepository",
]
