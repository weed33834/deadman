"""Dead Man Switch 数据模型

状态机：
    ACTIVE      用户按时 check-in
    SUSPECTED   连续 N 次失联（默认 3 次），启动多因子验证
    VERIFYING   邮件 + 短信 + 紧急联系人电话确认中
    CONFIRMED   紧急联系人确认 + 律师介入 + 继承人二次确认 + 7 天冷静期
    EXECUTED    动作已执行（发送身后信件 / 关闭数字账户 / 通知律师 / 通知继承人）
    CANCELLED   用户主动取消

PIPL 合规（第五章）：
    - email / phone 字段必须脱敏后存储（如 u***@example.com / 138****1234）
    - 加密原语复用 ending_note.store 的 _encrypt / _decrypt
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SwitchState(str, Enum):
    """Dead Man Switch 状态枚举（str 子类便于 JSON 序列化）"""

    ACTIVE = "ACTIVE"
    SUSPECTED = "SUSPECTED"
    VERIFYING = "VERIFYING"
    CONFIRMED = "CONFIRMED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


# ====================================================================
# PII 脱敏工具
# ====================================================================
def mask_email(email: str) -> str:
    """邮箱脱敏：u***@example.com

    PIPL 第五章：不存储原始 PII。脱敏后存盘。
    """
    if not email or "@" not in email:
        return email or ""
    local, _, domain = email.partition("@")
    if not local:
        return email
    head = local[0]
    return f"{head}***@{domain}"


def mask_phone(phone: str) -> str:
    """手机号脱敏：138****1234

    PIPL 第五章：不存储原始 PII。脱敏后存盘。
    """
    if not phone:
        return phone or ""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}****{digits[-4:]}"


# ====================================================================
# 配置数据结构
# ====================================================================
@dataclass
class SwitchConfig:
    """Dead Man Switch 用户配置

    所有时间字段单位为天。所有 PII 字段（email / phone）在 set_config 时
    自动脱敏，存储中不出现明文。
    """

    # check-in 频率（多少天一次 check-in 算"活跃"）。默认 30 天。
    check_in_frequency_days: int = 30
    # 连续多少次失联后进入 SUSPECTED。默认 3 次。
    missed_threshold: int = 3
    # 多因子验证窗口（多少天内回复"安好"算用户在）。默认 7 天。
    verification_window_days: int = 7
    # 冷静期天数（CONFIRMED 状态下不可执行，期间可撤销）。默认 7 天。
    cooldown_days: int = 7
    # 紧急联系人 user_id 列表（至少 1 名）
    emergency_contacts: list[str] = field(default_factory=list)
    # 律师 user_id（可选；不提供时跳过律师介入步骤）
    lawyer_user_id: str | None = None
    # 法定继承人 user_id 列表（至少 1 名才能从 VERIFYING 推进到 CONFIRMED）
    heir_user_ids: list[str] = field(default_factory=list)
    # 脱敏后的邮箱（如 u***@example.com）
    email_masked: str | None = None
    # 脱敏后的手机号（如 138****1234）
    phone_masked: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_in_frequency_days": self.check_in_frequency_days,
            "missed_threshold": self.missed_threshold,
            "verification_window_days": self.verification_window_days,
            "cooldown_days": self.cooldown_days,
            "emergency_contacts": list(self.emergency_contacts),
            "lawyer_user_id": self.lawyer_user_id,
            "heir_user_ids": list(self.heir_user_ids),
            "email_masked": self.email_masked,
            "phone_masked": self.phone_masked,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SwitchConfig:
        return cls(
            check_in_frequency_days=int(d.get("check_in_frequency_days", 30)),
            missed_threshold=int(d.get("missed_threshold", 3)),
            verification_window_days=int(d.get("verification_window_days", 7)),
            cooldown_days=int(d.get("cooldown_days", 7)),
            emergency_contacts=list(d.get("emergency_contacts", []) or []),
            lawyer_user_id=d.get("lawyer_user_id"),
            heir_user_ids=list(d.get("heir_user_ids", []) or []),
            email_masked=d.get("email_masked"),
            phone_masked=d.get("phone_masked"),
        )

    def set_email(self, email: str) -> None:
        """设置邮箱（自动脱敏后存盘）"""
        self.email_masked = mask_email(email)

    def set_phone(self, phone: str) -> None:
        """设置手机号（自动脱敏后存盘）"""
        self.phone_masked = mask_phone(phone)


# ====================================================================
# 主记录
# ====================================================================
@dataclass
class SwitchRecord:
    """Dead Man Switch 单个用户的完整记录

    文件级加密存储于 ~/.deadman/deadman_switch/{user_id}/switch.json
    """

    user_id: str
    config: SwitchConfig
    state: SwitchState = SwitchState.ACTIVE
    # 最近一次 check-in 时间（UTC）
    last_check_in: datetime | None = None
    # 最近一次失联计数起点（用于触发 SUSPECTED 阈值）
    last_missed: datetime | None = None
    # 失联次数（连续 N 次未 check-in 推进状态机）
    missed_count: int = 0
    # 多因子验证状态：每个联系人是否已确认失联
    contact_confirmations: dict[str, bool] = field(default_factory=dict)
    # contact_user_id -> confirmed_at（已确认时间）
    contact_confirmed_at: dict[str, str] = field(default_factory=dict)
    # 继承人确认状态
    heir_confirmations: dict[str, bool] = field(default_factory=dict)
    heir_confirmed_at: dict[str, str] = field(default_factory=dict)
    # 律师是否已介入
    lawyer_engaged: bool = False
    lawyer_engaged_at: str | None = None
    # 进入 CONFIRMED 状态的时间（冷静期起算）
    confirmed_at: datetime | None = None
    # 状态机历史：[{state, timestamp, reason}]
    state_history: list[dict[str, Any]] = field(default_factory=list)
    # 待执行动作（CONFIRMED -> EXECUTED 之间记录；EXECUTED 后清空，失败的留在这里重试）
    pending_actions: list[str] = field(default_factory=list)
    # 已执行动作日志：[{action, executed_at, result}]
    executed_actions: list[dict[str, Any]] = field(default_factory=list)
    # 记录创建时间
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "config": self.config.to_dict(),
            "state": self.state.value,
            "last_check_in": self.last_check_in.isoformat() if self.last_check_in else None,
            "last_missed": self.last_missed.isoformat() if self.last_missed else None,
            "missed_count": self.missed_count,
            "contact_confirmations": dict(self.contact_confirmations),
            "contact_confirmed_at": dict(self.contact_confirmed_at),
            "heir_confirmations": dict(self.heir_confirmations),
            "heir_confirmed_at": dict(self.heir_confirmed_at),
            "lawyer_engaged": self.lawyer_engaged,
            "lawyer_engaged_at": self.lawyer_engaged_at,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "state_history": list(self.state_history),
            "pending_actions": list(self.pending_actions),
            "executed_actions": list(self.executed_actions),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SwitchRecord:
        def _parse_dt(v: Any) -> datetime | None:
            if not v:
                return None
            try:
                return datetime.fromisoformat(v)
            except (TypeError, ValueError):
                return None

        return cls(
            user_id=d["user_id"],
            config=SwitchConfig.from_dict(d.get("config", {}) or {}),
            state=SwitchState(d.get("state", "ACTIVE")),
            last_check_in=_parse_dt(d.get("last_check_in")),
            last_missed=_parse_dt(d.get("last_missed")),
            missed_count=int(d.get("missed_count", 0) or 0),
            contact_confirmations=dict(d.get("contact_confirmations", {}) or {}),
            contact_confirmed_at=dict(d.get("contact_confirmed_at", {}) or {}),
            heir_confirmations=dict(d.get("heir_confirmations", {}) or {}),
            heir_confirmed_at=dict(d.get("heir_confirmed_at", {}) or {}),
            lawyer_engaged=bool(d.get("lawyer_engaged", False)),
            lawyer_engaged_at=d.get("lawyer_engaged_at"),
            confirmed_at=_parse_dt(d.get("confirmed_at")),
            state_history=list(d.get("state_history", []) or []),
            pending_actions=list(d.get("pending_actions", []) or []),
            executed_actions=list(d.get("executed_actions", []) or []),
            created_at=_parse_dt(d.get("created_at")) or datetime.now(timezone.utc).replace(tzinfo=None),
        )

    @classmethod
    def new(cls, user_id: str, config: SwitchConfig | None = None) -> SwitchRecord:
        """创建一条新的 switch 记录，初始状态 ACTIVE"""
        cfg = config or SwitchConfig()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rec = cls(
            user_id=user_id,
            config=cfg,
            state=SwitchState.ACTIVE,
            created_at=now,
            last_check_in=now,
        )
        rec.state_history.append(
            {
                "state": SwitchState.ACTIVE.value,
                "timestamp": now.isoformat(),
                "reason": "switch_initialized",
            }
        )
        return rec


# ====================================================================
# Check-in 日志
# ====================================================================
@dataclass
class CheckInLog:
    """单次 check-in 记录（用于审计 / 频率统计）

    存储路径：~/.deadman/deadman_switch/{user_id}/checkins.json
    """

    user_id: str
    check_in_at: datetime
    method: str  # web / email / sms / telegram / cli

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "check_in_at": self.check_in_at.isoformat(),
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CheckInLog:
        return cls(
            user_id=d["user_id"],
            check_in_at=datetime.fromisoformat(d["check_in_at"]),
            method=d.get("method", "web"),
        )
