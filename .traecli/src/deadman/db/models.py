"""ORM 模型 - 企业级扩展④ 主数据库

迁移优先级（按写竞争激烈程度排序）：
    1. users              — 全局单文件 read-modify-write，注册/更新即全文件重写
    2. cron_jobs          — 全局单文件，每次 tick 全文件重写
    3. notification_*     — 4 个全局文件，sent_log 无界增长

加密密文存储（VaultStore/SwitchStore/EndingNoteStore）留待扩展④b：
    原有 AES-256-GCM 密文以 LargeBinary 列存储，不解密、不改密钥派生。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


# =====================================================================
# users — 用户表（外键根，替代 auth/store.py 的 users.json）
# =====================================================================
class User(Base, TimestampMixin):
    """用户账户 - 对应 auth/store.py UserStore

    保留 email_hmac 唯一索引（防拖库撞库，与文件存储一致）。
    password_hash / salt 以 hex 字符串存储（与文件格式对齐，便于双向同步）。
    """

    __tablename__ = "users"

    # user_id（uuid4 字符串，与文件存储格式一致）
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # HMAC 索引：唯一约束 + 查询索引（防撞库 + O(1) 查找替代 O(N) 扫描）
    email_hmac: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)  # PBKDF2 hex
    salt: Mapped[str] = mapped_column(String(64), nullable=False)  # 随机盐 hex
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    family_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_users_family_id", "family_id"),
    )


# =====================================================================
# cron_jobs — Cron 调度任务（替代 cron/scheduler.py 的 jobs.json）
# =====================================================================
class CronJob(Base, TimestampMixin):
    """Cron 任务 - 对应 cron/scheduler.py CronJob

    将全局单文件拆为行级记录，消除全文件 read-modify-write 竞争。
    """

    __tablename__ = "cron_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    schedule: Mapped[str] = mapped_column(String(64), nullable=False)  # cron 表达式
    content: Mapped[str] = mapped_column(Text, nullable=False)  # 提醒内容
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="cron")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_fired: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    __table_args__ = (
        # 查询 enabled + 未过期的任务（tick 高频查询）
        Index("ix_cron_jobs_enabled_expires", "enabled", "expires_at"),
    )


# =====================================================================
# notification_* — 通知护栏状态（替代 notification/guardrail.py 的 4 个 JSON 文件）
# =====================================================================
class NotificationConsent(Base, TimestampMixin):
    """通知同意记录 - 对应 consent.json"""

    __tablename__ = "notification_consents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationUnsubscribe(Base, TimestampMixin):
    """退订记录 - 对应 unsubscribes.json"""

    __tablename__ = "notification_unsubscribes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationSentLog(Base):
    """发送日志 - 对应 sent_log.json（频率计数）

    文件版无界增长 + 每次全扫描计数；DB 版用索引 + 范围查询优化。
    """

    __tablename__ = "notification_sent_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # 频率检查：按 user_id + sent_at 范围扫描
        Index("ix_notification_sent_logs_user_sent", "user_id", "sent_at"),
    )


class NotificationLastSession(Base, TimestampMixin):
    """最近会话状态 - 对应 last_session.json（每用户单行）"""

    __tablename__ = "notification_last_sessions"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    safety_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    emotion_intensity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    involved_sensitive_death: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    user_birthday: Mapped[str | None] = mapped_column(String(10), nullable=True)  # MM-DD
    deceased_birthday: Mapped[str | None] = mapped_column(String(10), nullable=True)


# =====================================================================
# password_reset_tokens — 密码重置令牌（替代 auth/password_reset.py）
# =====================================================================
class PasswordResetToken(Base):
    """密码重置令牌 - 对应 auth/password_reset.py PasswordResetTokenStore"""

    __tablename__ = "password_reset_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


# =====================================================================
# 加密密文存储抽象（扩展④b 预留）
# =====================================================================
# VaultStore / SwitchStore / EndingNoteStore 的 AES-256-GCM 密文
# 将以 LargeBinary 列存储，保留 version 列以兼容 v1/v2/v3 解密路径。
# 此处仅声明通用基类，具体表结构在扩展④b 按各 store 的 envelope 细化。


__all__ = [
    "User",
    "CronJob",
    "NotificationConsent",
    "NotificationUnsubscribe",
    "NotificationSentLog",
    "NotificationLastSession",
    "PasswordResetToken",
]
