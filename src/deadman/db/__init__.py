"""主数据库层 - 企业级扩展④（PostgreSQL + SQLAlchemy 2.0 async + Alembic）

设计原则：
    1. **零侵入优雅降级**：DATABASE_URL 留空时，本层完全 no-op，
       所有现有文件存储（vault/auth/cron/...）原样工作，不影响单机/开发部署。
    2. **双写迁移过渡**：配置 DATABASE_URL 后，Repository 同时写 DB + 文件，
       读优先 DB、回退文件，实现零停机迁移。
    3. **加密密文原样迁移**：VaultStore/SwitchStore/EndingNoteStore 的 AES-256-GCM
       密文以 BYTEA 列存储，不解密、不改密钥派生，保证历史数据可恢复。
    4. **users 表为外键根**：所有 user_id 列指向 users.user_id。

模块：
    - base.py:      DeclarativeBase + 通用 mixin（TimestampMixin / UUIDPKMixin）
    - engine.py:    异步引擎 + 会话工厂（惰性初始化，DATABASE_URL 空时 no-op）
    - session.py:   FastAPI 依赖注入（get_db_session）
    - models.py:    ORM 模型（User / CronJob / NotificationConsent / ...）
    - repositories.py: Repository 协议 + 基类 + 具体实现
"""

from __future__ import annotations

from .base import Base, TimestampMixin, UUIDPKMixin
from .engine import db_enabled, get_async_session_factory, get_engine, init_db

__all__ = [
    "Base",
    "TimestampMixin",
    "UUIDPKMixin",
    "db_enabled",
    "get_async_session_factory",
    "get_engine",
    "init_db",
]
