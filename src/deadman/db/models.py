"""ORM 模型 - 企业级扩展④ 主数据库

迁移优先级（按写竞争激烈程度排序）：
    1. users              — 全局单文件 read-modify-write，注册/更新即全文件重写
    2. cron_jobs          — 全局单文件，每次 tick 全文件重写
    3. notification_*     — 4 个全局文件，sent_log 无界增长
    4. vault_items / switch_records / ending_note_* — 加密密文迁移（扩展④g/h/i）

加密密文存储（VaultStore/SwitchStore/EndingNoteStore）：
    原有 AES-256-GCM 密文以 LargeBinary/Text 列存储，不解密、不改密钥派生，
    保证历史数据可恢复。envelope 整体序列化为 JSON 字符串入库（保留 v1/v2/v3
    兼容解密路径所需的全部字段：version/nonce/salt/ct/tag）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    LargeBinary,
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

    __table_args__ = (Index("ix_users_family_id", "family_id"),)


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
    pending_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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
    involved_sensitive_death: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
# vault_items — 数字遗产保险库（替代 vault/store.py 的 .enc + index.json）
# =====================================================================
# 扩展④g：VaultStore 加密密文 DB 迁移
#   - content_encrypted 以 LargeBinary 原样存（AES-256-GCM envelope bytes）
#   - metadata / beneficiary_user_ids / delivered_to 用 JSON 列（与文件 index 对齐）
#   - 不解密、不改密钥派生，历史数据可恢复
class VaultItem(Base, TimestampMixin):
    """保险库条目 - 对应 vault/store.py VaultItem

    content_encrypted 为 AES-256-GCM envelope 原始字节，与 .enc 文件内容一致。
    """

    __tablename__ = "vault_items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 'metadata' 是 SQLAlchemy Declarative 保留属性名，Python 属性用 item_metadata，
    # 数据库列名仍为 metadata（与文件存储 index.json 字段对齐）
    item_metadata: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    beneficiary_user_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    delivery_trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivery_pending_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_to: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        # beneficiary 反查：按受益人列条目
        Index("ix_vault_items_owner_type", "owner_user_id", "type"),
    )


# =====================================================================
# switch_records / switch_check_ins — Dead Man Switch（替代 switch.json + checkins.json）
# =====================================================================
# 扩展④h：SwitchStore 加密密文 DB 迁移
#   - envelope 整体序列化为 JSON 字符串存 Text 列（保留 v1/v2/v3 解密所需字段）
#   - checkins 单独成表，避免无界增长的 JSON 数组拖慢 load
class SwitchRecord(Base, TimestampMixin):
    """Dead Man Switch 主记录 - 对应 deadman_switch/store.py SwitchRecord

    envelope_text 为加密 envelope 的 JSON 序列化字符串（与 switch.json 内容一致），
    不解密、不改密钥派生。每用户一行（user_id 为主键）。
    """

    __tablename__ = "switch_records"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    envelope_text: Mapped[str] = mapped_column(Text, nullable=False)


class SwitchCheckIn(Base):
    """Check-in 日志 - 对应 checkins.json（追加写、最近 200 条）

    文件版每次 record_check_in 都全量重写 checkins.json；
    DB 版改为 INSERT，消除读改写竞争，且支持按时间倒序索引查询。
    """

    __tablename__ = "switch_check_ins"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    check_in_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False, default="web")

    __table_args__ = (
        # 按用户倒序查最近 N 条
        Index("ix_switch_check_ins_user_time", "user_id", "check_in_at"),
    )


# =====================================================================
# ending_note_* — 终活笔记（替代 note.json + shares/incoming/pending_deliveries）
# =====================================================================
# 扩展④i：EndingNoteStore 加密密文 DB 迁移
#   - note envelope 整体序列化为 JSON 字符串存 Text 列
#   - shares/incoming/pending_deliveries 拆为行级记录，消除全文件重写
class EndingNoteRecord(Base, TimestampMixin):
    """终活笔记主体 - 对应 ending_note/store.py note.json

    envelope_text 为加密 envelope 的 JSON 序列化字符串（与 note.json 内容一致），
    不解密、不改密钥派生。每用户一行。
    """

    __tablename__ = "ending_note_records"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    envelope_text: Mapped[str] = mapped_column(Text, nullable=False)


class EndingNoteShare(Base, TimestampMixin):
    """笔记共享记录 - 对应 shares.json

    一行 = 一次 owner → target 共享关系（去重 upsert）。
    sections 为 None 表示共享全部章节。
    """

    __tablename__ = "ending_note_shares"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # f"{owner}:{target}"
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sections: Mapped[list | None] = mapped_column(JSON, nullable=True)
    shared_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EndingNoteIncoming(Base, TimestampMixin):
    """笔记接收记录 - 对应 incoming.json

    一行 = 一次 target ← owner 接收关系（去重 upsert，与 shares 镜像）。
    """

    __tablename__ = "ending_note_incoming"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # f"{target}:{owner}"
    target_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sections: Mapped[list | None] = mapped_column(JSON, nullable=True)
    shared_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EndingNotePendingDelivery(Base, TimestampMixin):
    """待投递记录 - 对应 pending_deliveries.json

    一行 = 一次投递触发（death_confirmation/date/manual）。
    状态机：pending → ready → delivered。
    """

    __tablename__ = "ending_note_pending_deliveries"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )  # f"{owner}:{trigger}:{triggered_at}"
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deliver_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recipients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# =====================================================================
# customers / cases / case_events — 机构客户档案与案件（B2B-IMPLEMENTATION Step 5）
# =====================================================================
# 对齐 B2B-TECH-DESIGN §3.3–3.4：客户档案 + 案件 + 事件（兼作审计）。
# org_id 是硬隔离键，所有查询必须同时带 org_id + 主键（防跨租户越权）。
# relationships / tags / detail 用 JSON 列（与文件版 org/file_customers.py 对齐）。
class Customer(Base, TimestampMixin):
    """机构客户档案 - 对应 /api/org/customers

    org_id + id 双键定位；任何按 id 查询都必须带 org_id 校验归属。
    """

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    province: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="planning")
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    relationships: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        # 客户列表高频查询：按机构 + 主办人
        Index("ix_customers_org_owner", "org_id", "owner_user_id"),
    )


class Case(Base, TimestampMixin):
    """机构案件 - 对应 /api/org/cases

    status 状态机见 org/case_flow.py CASE_FLOW；状态变更必须落 case_events。
    """

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False, default="funeral")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    assignee_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 客户下的案件列表（高频） + 待办（assignee + status）
        Index("ix_cases_org_customer", "org_id", "customer_id"),
        Index("ix_cases_org_assignee", "org_id", "assignee_user_id", "status"),
    )


class CaseEvent(Base):
    """案件事件 - 兼作审计（谁/何时/对哪个案件/做了什么）

    只增不改；状态变更、分配、材料生成都落这里。
    """

    __tablename__ = "case_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )

    __table_args__ = (
        # 按案件倒序查时间线
        Index("ix_case_events_case_time", "case_id", "created_at"),
    )


__all__ = [
    "User",
    "CronJob",
    "NotificationConsent",
    "NotificationUnsubscribe",
    "NotificationSentLog",
    "NotificationLastSession",
    "PasswordResetToken",
    "VaultItem",
    "SwitchRecord",
    "SwitchCheckIn",
    "EndingNoteRecord",
    "EndingNoteShare",
    "EndingNoteIncoming",
    "EndingNotePendingDelivery",
    "Customer",
    "Case",
    "CaseEvent",
]
