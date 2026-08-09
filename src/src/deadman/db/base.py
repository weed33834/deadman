"""ORM 基类与通用 mixin - SQLAlchemy 2.0 declarative 风格"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。Alembic autogenerate 由此派生表结构。"""


class UUIDPKMixin:
    """UUID 主键 mixin - 与现有文件存储的 uuid4 hex ID 保持一致。

    使用 String(36) 而非 PG 原生 UUID 类型，保证跨数据库可移植性
    （SQLite 测试 / MySQL 兼容），且与现有 user_id/job_id 格式（uuid4 字符串）对齐。
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True)


class TimestampMixin:
    """created_at / updated_at 自动维护时间戳（UTC naive，与现有代码一致）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )
