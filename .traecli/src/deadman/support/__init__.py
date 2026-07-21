"""客服工单系统 - Phase 16C

提供客服工单的创建、查询、回复、状态流转能力。
遵守 PIPL 第 19 条数据保留期限（已关闭工单保留 2 年）。

模块组成：
- models.py：Ticket / TicketReply 数据类
- store.py：TicketStore 原子文件写入
"""

from __future__ import annotations

from .models import TicketReply, Ticket, TicketStatus
from .store import TicketStore

__all__ = [
    "TicketReply",
    "Ticket",
    "TicketStatus",
    "TicketStore",
]
