"""测试 deadman.support - Phase 16C 客服工单系统

覆盖点（>= 12 个）：
  - TicketStore CRUD：create / get / list_user / add_reply / update_status
  - user_id 越权防护（get_ticket 越权返回 None）
  - add_reply 追加
  - status 流转（合法 + 非法）
  - 校验：category / priority / subject / description
  - 文件权限 0o600
  - 原子写入（index 与 ticket 文件并存）
  - list_all_tickets（管理员视角）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deadman.support.models import Ticket, TicketReply, TicketStatus
from deadman.support.store import TicketStore

# =====================================================================
# 辅助
# =====================================================================


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    return TicketStore(data_dir=tmp_path)


def _make_ticket(
    store: TicketStore,
    user_id: str = "user-001",
    category: str = "咨询",
    priority: str = "普通",
    subject: str = "如何办理户口注销？",
    description: str = "请问户口注销需要哪些材料？",
) -> Ticket:
    return store.create_ticket(user_id, category, priority, subject, description)


# =====================================================================
# 1. create + get 基础 CRUD
# =====================================================================


class TestTicketCreateRead:
    def test_create_returns_ticket_with_id(self, store: TicketStore):
        t = _make_ticket(store)
        assert t.ticket_id.startswith("tkt-")
        assert len(t.ticket_id) == len("tkt-") + 12
        assert t.status == TicketStatus.OPEN.value
        assert t.user_id == "user-001"

    def test_get_ticket_returns_same_data(self, store: TicketStore):
        t = _make_ticket(store)
        loaded = store.get_ticket(t.ticket_id, user_id="user-001")
        assert loaded is not None
        assert loaded.ticket_id == t.ticket_id
        assert loaded.subject == "如何办理户口注销？"

    def test_get_nonexistent_returns_none(self, store: TicketStore):
        assert store.get_ticket("tkt-nonexistent", user_id="user-001") is None

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX 文件权限位在 Windows 无语义"
    )
    def test_ticket_file_written_with_0o600(self, store: TicketStore):
        t = _make_ticket(store)
        path = store.tickets_dir / f"{t.ticket_id}.json"
        assert path.exists()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"文件权限应为 0o600，实际 0o{mode:o}"

    def test_index_file_written(self, store: TicketStore):
        t = _make_ticket(store)
        idx = store._read_index()
        assert t.ticket_id in idx
        assert idx[t.ticket_id]["user_id"] == "user-001"
        assert idx[t.ticket_id]["subject"] == "如何办理户口注销？"


# =====================================================================
# 2. user_id 越权防护
# =====================================================================


class TestOwnershipEnforcement:
    def test_get_ticket_other_user_returns_none(self, store: TicketStore):
        t = _make_ticket(store, user_id="alice")
        # bob 试图读取 alice 的工单
        loaded = store.get_ticket(t.ticket_id, user_id="bob")
        assert loaded is None, "越权访问应返回 None"

    def test_add_reply_other_user_returns_none(self, store: TicketStore):
        t = _make_ticket(store, user_id="alice")
        reply = store.add_reply(
            ticket_id=t.ticket_id,
            author="user",
            content="恶意回复",
            user_id="bob",
        )
        assert reply is None

    def test_update_status_other_user_returns_false(self, store: TicketStore):
        t = _make_ticket(store, user_id="alice")
        ok = store.update_status(
            ticket_id=t.ticket_id,
            status=TicketStatus.IN_PROGRESS.value,
            user_id="bob",
        )
        assert ok is False


# =====================================================================
# 3. add_reply 追加
# =====================================================================


class TestAddReply:
    def test_add_reply_user_role(self, store: TicketStore):
        t = _make_ticket(store)
        reply = store.add_reply(
            ticket_id=t.ticket_id,
            author="user",
            content="补充：逝者在北京",
            user_id="user-001",
        )
        assert reply is not None
        assert reply.reply_id.startswith("rep-")
        assert reply.author == "user"

    def test_add_reply_staff_role(self, store: TicketStore):
        t = _make_ticket(store)
        # staff 回复不带 user_id（管理员视角）
        reply = store.add_reply(
            ticket_id=t.ticket_id,
            author="staff",
            content="已受理，请补充更多信息",
        )
        assert reply is not None
        assert reply.author == "staff"

    def test_add_reply_invalid_author_raises(self, store: TicketStore):
        t = _make_ticket(store)
        # add_reply 内部走 TicketReply.new 会抛 ValueError
        # 但 store 没捕获 ValueError，会向上抛
        with pytest.raises(ValueError):
            store.add_reply(
                ticket_id=t.ticket_id,
                author="unknown",
                content="非法作者",
                user_id="user-001",
            )

    def test_add_reply_empty_content_raises(self, store: TicketStore):
        t = _make_ticket(store)
        with pytest.raises(ValueError):
            store.add_reply(
                ticket_id=t.ticket_id,
                author="user",
                content="",
                user_id="user-001",
            )

    def test_replies_persist_after_reload(self, store: TicketStore):
        t = _make_ticket(store)
        store.add_reply(t.ticket_id, "user", "第一条回复", "user-001")
        store.add_reply(t.ticket_id, "staff", "客服回复")
        loaded = store.get_ticket(t.ticket_id, "user-001")
        assert loaded is not None
        assert len(loaded.replies) == 2
        assert loaded.replies[0].content == "第一条回复"
        assert loaded.replies[1].content == "客服回复"


# =====================================================================
# 4. status 流转
# =====================================================================


class TestStatusTransition:
    def test_open_to_in_progress(self, store: TicketStore):
        t = _make_ticket(store)
        ok = store.update_status(t.ticket_id, TicketStatus.IN_PROGRESS.value, "user-001")
        assert ok is True
        loaded = store.get_ticket(t.ticket_id, "user-001")
        assert loaded.status == "in_progress"

    def test_open_to_resolved_skips_in_progress(self, store: TicketStore):
        t = _make_ticket(store)
        ok = store.update_status(t.ticket_id, TicketStatus.RESOLVED.value, "user-001")
        assert ok is True
        loaded = store.get_ticket(t.ticket_id, "user-001")
        assert loaded.status == "resolved"
        assert loaded.resolved_at is not None

    def test_closed_is_terminal(self, store: TicketStore):
        t = _make_ticket(store)
        store.update_status(t.ticket_id, TicketStatus.RESOLVED.value, "user-001")
        store.update_status(t.ticket_id, TicketStatus.CLOSED.value, "user-001")
        # closed -> open 不允许
        ok = store.update_status(t.ticket_id, TicketStatus.OPEN.value, "user-001")
        assert ok is False

    def test_invalid_transition_returns_false(self, store: TicketStore):
        t = _make_ticket(store)
        # open -> closed 直接关闭不允许？实际上允许（任意状态可 closed）
        # 改测：open -> in_progress -> open 倒退不允许
        store.update_status(t.ticket_id, TicketStatus.IN_PROGRESS.value, "user-001")
        ok = store.update_status(t.ticket_id, TicketStatus.OPEN.value, "user-001")
        assert ok is False

    def test_invalid_status_value_returns_false(self, store: TicketStore):
        t = _make_ticket(store)
        ok = store.update_status(t.ticket_id, "invalid_status", "user-001")
        assert ok is False


# =====================================================================
# 5. 字段校验
# =====================================================================


class TestTicketValidation:
    def test_invalid_category_raises(self, store: TicketStore):
        with pytest.raises(ValueError, match="category"):
            store.create_ticket("user-001", "无效类别", "普通", "标题", "描述")

    def test_invalid_priority_raises(self, store: TicketStore):
        with pytest.raises(ValueError, match="priority"):
            store.create_ticket("user-001", "咨询", "无效优先级", "标题", "描述")

    def test_empty_subject_raises(self, store: TicketStore):
        with pytest.raises(ValueError, match="subject"):
            store.create_ticket("user-001", "咨询", "普通", "", "描述")

    def test_subject_too_long_raises(self, store: TicketStore):
        with pytest.raises(ValueError, match="subject"):
            store.create_ticket("user-001", "咨询", "普通", "x" * 201, "描述")

    def test_empty_description_raises(self, store: TicketStore):
        with pytest.raises(ValueError, match="description"):
            store.create_ticket("user-001", "咨询", "普通", "标题", "")

    def test_empty_user_id_raises(self, store: TicketStore):
        with pytest.raises(ValueError, match="user_id"):
            store.create_ticket("", "咨询", "普通", "标题", "描述")


# =====================================================================
# 6. list_user_tickets / list_all_tickets
# =====================================================================


class TestListTickets:
    def test_list_user_tickets_only_returns_own(self, store: TicketStore):
        _make_ticket(store, user_id="alice", subject="A1")
        _make_ticket(store, user_id="bob", subject="B1")
        _make_ticket(store, user_id="alice", subject="A2")
        alice_tickets = store.list_user_tickets("alice")
        assert len(alice_tickets) == 2
        for t in alice_tickets:
            assert t.user_id == "alice"

    def test_list_user_tickets_empty(self, store: TicketStore):
        tickets = store.list_user_tickets("nobody")
        assert tickets == []

    def test_list_all_tickets_returns_all_users(self, store: TicketStore):
        _make_ticket(store, user_id="alice", subject="A1")
        _make_ticket(store, user_id="bob", subject="B1")
        all_tickets = store.list_all_tickets()
        assert len(all_tickets) == 2
        user_ids = {t.user_id for t in all_tickets}
        assert user_ids == {"alice", "bob"}

    def test_list_user_tickets_sorted_desc_by_created(self, store: TicketStore):
        import time

        _make_ticket(store, user_id="alice", subject="第一")
        time.sleep(0.01)
        _make_ticket(store, user_id="alice", subject="第二")
        tickets = store.list_user_tickets("alice")
        # 倒序：第二在前
        assert tickets[0].subject == "第二"
        assert tickets[1].subject == "第一"


# =====================================================================
# 7. 序列化往返
# =====================================================================


class TestSerialization:
    def test_ticket_to_dict_roundtrip(self, store: TicketStore):
        t = _make_ticket(store)
        store.add_reply(t.ticket_id, "user", "回复1", "user-001")
        loaded = store.get_ticket(t.ticket_id, "user-001")
        d = loaded.to_dict()
        # 反序列化
        t2 = Ticket.from_dict(d)
        assert t2.ticket_id == loaded.ticket_id
        assert t2.subject == loaded.subject
        assert len(t2.replies) == 1
        assert t2.replies[0].content == "回复1"

    def test_reply_to_dict_roundtrip(self):
        r = TicketReply.new("user", "测试回复")
        d = r.to_dict()
        r2 = TicketReply.from_dict(d)
        assert r2.content == r.content
        assert r2.author == r.author
        assert r2.reply_id == r.reply_id
