"""客服工单存储 - Phase 16C

参考 auth/store.py 原子写入模式：
- 单工单文件：~/.deadman/support/tickets/{ticket_id}.json（权限 0o600）
- 索引文件：~/.deadman/support/index.json（ticket_id → 简要信息）

CRUD：
- create_ticket(user_id, category, priority, subject, description) -> Ticket
- get_ticket(ticket_id, user_id) -> Ticket | None   # user_id 越权返回 None
- list_user_tickets(user_id) -> list[Ticket]
- add_reply(ticket_id, author, content) -> TicketReply
- update_status(ticket_id, status) -> bool
- list_all_tickets() -> list[Ticket]   # 管理员视角，测试用
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from .models import Ticket, TicketReply

_DEFAULT_DATA_DIR = Path(
    os.getenv("DEADMAN_SUPPORT_DATA_DIR", str(Path.home() / ".deadman" / "support"))
)


class TicketStore:
    """客服工单存储 - 原子文件写入

    存储结构：
        ~/.deadman/support/
        ├── tickets/
        │   ├── tkt-abc123.json     # 单工单完整数据
        │   └── tkt-def456.json
        └── index.json               # 索引：ticket_id → 简要信息

    安全：
    - 文件权限 0o600（仅 owner 读写）
    - 原子写入：先 .tmp 再 os.replace
    - user_id 越权访问返回 None
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir: Path = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.tickets_dir: Path = self.data_dir / "tickets"
        self.index_file: Path = self.data_dir / "index.json"
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        # data_dir 本身权限设为 0o700
        try:
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.tickets_dir, 0o700)
        except OSError:
            pass

    # ============================================================
    # 公开 API
    # ============================================================

    def create_ticket(
        self,
        user_id: str,
        category: str,
        priority: str,
        subject: str,
        description: str,
    ) -> Ticket:
        """创建工单（含校验），写入文件 + 更新索引"""
        ticket = Ticket.new(user_id, category, priority, subject, description)
        self._write_ticket(ticket)
        self._update_index(ticket)
        return ticket

    def get_ticket(self, ticket_id: str, user_id: str) -> Ticket | None:
        """获取工单详情

        - 越权访问（user_id 不匹配）返回 None
        - 工单不存在返回 None
        """
        ticket = self._read_ticket(ticket_id)
        if ticket is None:
            return None
        if ticket.user_id != user_id:
            # 越权访问：不返回数据
            return None
        return ticket

    def list_user_tickets(self, user_id: str) -> list[Ticket]:
        """列出某用户的所有工单（按创建时间倒序）"""
        index = self._read_index()
        ticket_ids = [
            tid for tid, info in index.items()
            if info.get("user_id") == user_id
        ]
        tickets: list[Ticket] = []
        for tid in ticket_ids:
            t = self._read_ticket(tid)
            if t is not None:
                tickets.append(t)
        tickets.sort(key=lambda t: t.created_at, reverse=True)
        return tickets

    def list_all_tickets(self) -> list[Ticket]:
        """列出所有工单（管理员视角，测试用）

        生产环境需上层调用方校验当前请求者 role=admin。
        """
        index = self._read_index()
        tickets: list[Ticket] = []
        for tid in index:
            t = self._read_ticket(tid)
            if t is not None:
                tickets.append(t)
        tickets.sort(key=lambda t: t.created_at, reverse=True)
        return tickets

    def add_reply(
        self, ticket_id: str, author: str, content: str, user_id: str | None = None
    ) -> TicketReply | None:
        """给工单追加一条回复

        - 工单不存在返回 None
        - user_id 给定时校验越权（不匹配返回 None）
        - author 必须是 user / staff
        """
        ticket = self._read_ticket(ticket_id)
        if ticket is None:
            return None
        if user_id is not None and ticket.user_id != user_id:
            return None
        reply = ticket.add_reply(author, content)
        self._write_ticket(ticket)
        self._update_index(ticket)
        return reply

    def update_status(
        self, ticket_id: str, status: str, user_id: str | None = None
    ) -> bool:
        """更新工单状态

        - 工单不存在返回 False
        - 状态流转不合法返回 False
        - user_id 给定时校验越权（不匹配返回 False）
        """
        ticket = self._read_ticket(ticket_id)
        if ticket is None:
            return False
        if user_id is not None and ticket.user_id != user_id:
            return False
        ok = ticket.transition_to(status)
        if not ok:
            return False
        self._write_ticket(ticket)
        self._update_index(ticket)
        return True

    # ============================================================
    # 内部工具
    # ============================================================

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / f"{ticket_id}.json"

    def _write_ticket(self, ticket: Ticket) -> None:
        """原子写入单工单文件（权限 0o600）"""
        path = self._ticket_path(ticket.ticket_id)
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
            # 设置文件权限 0o600
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

    def _read_ticket(self, ticket_id: str) -> Ticket | None:
        """读取单工单文件"""
        path = self._ticket_path(ticket_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Ticket.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def _read_index(self) -> dict[str, dict[str, Any]]:
        """读取索引文件，不存在返回空 dict"""
        if not self.index_file.exists():
            return {}
        try:
            return json.loads(self.index_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _update_index(self, ticket: Ticket) -> None:
        """更新索引：ticket_id → 简要信息

        简要信息：{user_id, category, priority, subject, status, created_at, updated_at}
        """
        index = self._read_index()
        index[ticket.ticket_id] = {
            "user_id": ticket.user_id,
            "category": ticket.category,
            "priority": ticket.priority,
            "subject": ticket.subject,
            "status": ticket.status,
            "created_at": ticket.created_at,
            "updated_at": ticket.updated_at,
        }
        self._atomic_write_index(index)

    def _atomic_write_index(self, index: dict[str, dict[str, Any]]) -> None:
        """原子写入索引文件"""
        tmp_path = self.index_file.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(index, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.index_file)
            with contextlib.suppress(OSError):
                os.chmod(self.index_file, 0o600)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
