"""客服工单数据模型 - Phase 16C

数据类：
- TicketReply：单条回复（user / staff 发起）
- Ticket：完整工单，含回复列表

字段语义参见 docs/support.md「工单状态流转」。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class TicketStatus(str, enum.Enum):
    """工单状态机：open → in_progress → resolved → closed

    允许的流转：
      open -> in_progress -> resolved -> closed
      open -> resolved -> closed （客服直接解决）
      任意状态 -> closed （用户取消）
    """

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketReplyAuthor(str, enum.Enum):
    """回复作者类型"""

    USER = "user"
    STAFF = "staff"


def _utcnow_iso() -> str:
    """UTC ISO 时间戳"""
    return datetime.now(timezone.utc).isoformat()


def _gen_reply_id() -> str:
    """生成 reply_id：rep-{uuid12}"""
    return f"rep-{uuid.uuid4().hex[:12]}"


@dataclass
class TicketReply:
    """工单回复（单条）"""

    reply_id: str
    author: str  # "user" | "staff"
    content: str
    created_at: str  # ISO 时间戳

    @classmethod
    def new(
        cls, author: str, content: str, created_at: str | None = None
    ) -> "TicketReply":
        """创建一条新回复

        author 校验：必须是 TicketReplyAuthor 枚举值之一
        """
        if author not in {a.value for a in TicketReplyAuthor}:
            raise ValueError(f"author 必须是 user 或 staff，收到: {author}")
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        return cls(
            reply_id=_gen_reply_id(),
            author=author,
            content=content.strip(),
            created_at=created_at or _utcnow_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply_id": self.reply_id,
            "author": self.author,
            "content": self.content,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TicketReply":
        return cls(
            reply_id=data["reply_id"],
            author=data["author"],
            content=data["content"],
            created_at=data["created_at"],
        )


@dataclass
class Ticket:
    """完整工单"""

    ticket_id: str
    user_id: str
    category: str  # 咨询 / 反馈 / 投诉 / 数据删除 / 跨境合规
    priority: str  # 低 / 普通 / 紧急
    subject: str
    description: str
    status: str  # open / in_progress / resolved / closed
    created_at: str
    updated_at: str
    resolved_at: str | None = None
    assigned_to: str | None = None  # 客服 staff_id
    replies: list[TicketReply] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        user_id: str,
        category: str,
        priority: str,
        subject: str,
        description: str,
    ) -> "Ticket":
        """创建新工单

        校验：
        - user_id 非空
        - category 在允许集合内
        - priority 在允许集合内
        - subject 非空且 <= 200 字符
        - description 非空且 <= 5000 字符
        """
        if not user_id or not isinstance(user_id, str):
            raise ValueError("user_id 不能为空")
        if category not in _ALLOWED_CATEGORIES:
            raise ValueError(
                f"category 必须是 {sorted(_ALLOWED_CATEGORIES)} 之一，收到: {category}"
            )
        if priority not in _ALLOWED_PRIORITIES:
            raise ValueError(
                f"priority 必须是 {sorted(_ALLOWED_PRIORITIES)} 之一，收到: {priority}"
            )
        if not subject or not subject.strip():
            raise ValueError("subject 不能为空")
        if len(subject) > 200:
            raise ValueError("subject 长度不能超过 200")
        if not description or not description.strip():
            raise ValueError("description 不能为空")
        if len(description) > 5000:
            raise ValueError("description 长度不能超过 5000")

        now = _utcnow_iso()
        ticket_id = f"tkt-{uuid.uuid4().hex[:12]}"
        return cls(
            ticket_id=ticket_id,
            user_id=user_id,
            category=category,
            priority=priority,
            subject=subject.strip(),
            description=description.strip(),
            status=TicketStatus.OPEN.value,
            created_at=now,
            updated_at=now,
            resolved_at=None,
            assigned_to=None,
            replies=[],
        )

    def add_reply(self, author: str, content: str) -> TicketReply:
        """追加一条回复并更新 updated_at"""
        reply = TicketReply.new(author, content)
        self.replies.append(reply)
        self.updated_at = _utcnow_iso()
        return reply

    def transition_to(self, new_status: str) -> bool:
        """状态流转

        返回 True 表示流转成功，False 表示不允许的流转
        """
        new_status_lower = new_status.lower()
        if new_status_lower not in {s.value for s in TicketStatus}:
            return False
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status_lower not in allowed:
            return False
        self.status = new_status_lower
        self.updated_at = _utcnow_iso()
        if new_status_lower == TicketStatus.RESOLVED.value:
            self.resolved_at = self.updated_at
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "user_id": self.user_id,
            "category": self.category,
            "priority": self.priority,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "assigned_to": self.assigned_to,
            "replies": [r.to_dict() for r in self.replies],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ticket":
        replies = [TicketReply.from_dict(r) for r in data.get("replies", [])]
        return cls(
            ticket_id=data["ticket_id"],
            user_id=data["user_id"],
            category=data["category"],
            priority=data["priority"],
            subject=data["subject"],
            description=data["description"],
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            resolved_at=data.get("resolved_at"),
            assigned_to=data.get("assigned_to"),
            replies=replies,
        )


# ============================================================
# 常量
# ============================================================

_ALLOWED_CATEGORIES = {
    "咨询",
    "反馈",
    "投诉",
    "数据删除",
    "跨境合规",
}

_ALLOWED_PRIORITIES = {
    "低",
    "普通",
    "紧急",
}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TicketStatus.OPEN.value: {
        TicketStatus.IN_PROGRESS.value,
        TicketStatus.RESOLVED.value,
        TicketStatus.CLOSED.value,
    },
    TicketStatus.IN_PROGRESS.value: {
        TicketStatus.RESOLVED.value,
        TicketStatus.CLOSED.value,
    },
    TicketStatus.RESOLVED.value: {
        TicketStatus.CLOSED.value,
        TicketStatus.IN_PROGRESS.value,  # 用户重开
    },
    TicketStatus.CLOSED.value: set(),  # 终态
}
