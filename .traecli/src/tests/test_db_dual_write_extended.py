"""企业级扩展④f/④g/④h/④i DB 双写测试

覆盖：
    - NotificationGuardrail 4 个 record_* 方法 DB 双写（扩展④f）
    - VaultStore 加密密文 DB 迁移（扩展④g）
    - SwitchStore 加密密文 DB 迁移（扩展④h）
    - EndingNoteStore 加密密文 DB 迁移（扩展④i）

使用 aiosqlite 内存库，与 test_db_layer.py 相同的 fixture 模式。
fire-and-forget DB 同步用 _await_bg_sync 等待完成。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from deadman.db import engine as db_engine_mod
from deadman.db.engine import dispose_engine
from sqlalchemy import select

# =====================================================================
# Fixtures（与 test_db_layer.py 一致）
# =====================================================================

@pytest.fixture
async def sqlite_db(monkeypatch):
    """配置 SQLite 内存库。"""
    from deadman.config import settings

    old_url = settings.database_url
    settings.database_url = "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true"
    await dispose_engine()
    yield settings
    await dispose_engine()
    settings.database_url = old_url


@pytest.fixture
async def initialized_db(sqlite_db):
    """初始化表结构（create_all）。"""
    from deadman.db.engine import init_db

    await init_db()
    return sqlite_db


async def _await_bg_sync(timeout: float = 5.0):
    """等待 fire-and-forget DB 同步后台任务完成。

    生产端（SwitchStore._run_async 等）用 asyncio.ensure_future() 派发同步协程
    且刻意不保留引用，测试侧无法直接 await 具体 task。

    这里轮询 asyncio.all_tasks() 等待除当前任务外的全部任务结束，而不是固定
    sleep 一个经验值——固定睡眠在 CI / 高负载机器上会因后台任务尚未完成而间歇
    性失败（本函数原为 sleep(0.2)，实测约 1/3 概率挂在 record_check_in 断言）。
    轮询方式在空闲时立即返回，更快；在负载高时最长等到 timeout，更稳。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    current = asyncio.current_task()
    while True:
        pending = {t for t in asyncio.all_tasks() if t is not current and not t.done()}
        if not pending:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            # 超时兜底：不在此处抛错，交由调用方的业务断言暴露真实问题
            return
        await asyncio.wait(pending, timeout=remaining)


# =====================================================================
# 扩展④f：NotificationGuardrail DB 双写
# =====================================================================

class TestNotificationGuardrailDualWrite:
    """验证 NotificationGuardrail 4 个 record_* 方法在 DB 启用时双写。"""

    async def test_record_consent_syncs_to_db(self, initialized_db, tmp_path):
        from deadman.db.models import NotificationConsent
        from deadman.notification.guardrail import NotificationGuardrail

        guard = NotificationGuardrail(data_dir=tmp_path / "notif")
        guard.record_consent("user-n1", "同意推送", "reminder:2026-07-30")

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            stmt = select(NotificationConsent).where(
                NotificationConsent.user_id == "user-n1"
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            assert row is not None
            assert row.scope == "reminder:2026-07-30"
            assert row.content == "同意推送"

    async def test_record_unsubscribe_syncs_to_db(self, initialized_db, tmp_path):
        from deadman.db.models import NotificationUnsubscribe
        from deadman.notification.guardrail import NotificationGuardrail

        guard = NotificationGuardrail(data_dir=tmp_path / "notif")
        guard.record_unsubscribe("user-n2", scope="all")

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            stmt = select(NotificationUnsubscribe).where(
                NotificationUnsubscribe.user_id == "user-n2"
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            assert row is not None
            assert row.scope == "all"

    async def test_record_send_syncs_to_db(self, initialized_db, tmp_path):
        from deadman.db.models import NotificationSentLog
        from deadman.notification.guardrail import NotificationGuardrail

        guard = NotificationGuardrail(data_dir=tmp_path / "notif")
        sent_at = datetime.now()
        guard.record_send("user-n3", "脱敏内容", "telegram", sent_at=sent_at)

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            stmt = select(NotificationSentLog).where(
                NotificationSentLog.user_id == "user-n3"
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            assert row is not None
            assert row.channel == "telegram"
            assert row.content == "脱敏内容"

    async def test_record_session_end_syncs_to_db(self, initialized_db, tmp_path):
        from deadman.db.models import NotificationLastSession
        from deadman.notification.guardrail import NotificationGuardrail

        guard = NotificationGuardrail(data_dir=tmp_path / "notif")
        guard.record_session_end(
            "user-n4",
            safety_triggered=True,
            emotion_intensity="高",
            involved_sensitive_death=False,
        )

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            row = await session.get(NotificationLastSession, "user-n4")
            assert row is not None
            assert row.safety_triggered is True
            assert row.emotion_intensity == 3.0  # "高" → 3.0
            assert row.involved_sensitive_death is False

    async def test_record_session_end_upsert(self, initialized_db, tmp_path):
        """二次 record_session_end 应 UPSERT 而非报错。"""
        from deadman.db.models import NotificationLastSession
        from deadman.notification.guardrail import NotificationGuardrail

        guard = NotificationGuardrail(data_dir=tmp_path / "notif")
        guard.record_session_end("user-n5", False, "低", False)
        await _await_bg_sync()
        guard.record_session_end("user-n5", True, "高", True)
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            stmt = select(NotificationLastSession).where(
                NotificationLastSession.user_id == "user-n5"
            )
            rows = (await session.execute(stmt)).scalars().all()
            assert len(rows) == 1  # UPSERT，不应有两条
            assert rows[0].safety_triggered is True
            assert rows[0].emotion_intensity == 3.0


# =====================================================================
# 扩展④g：VaultStore DB 双写
# =====================================================================

class TestVaultStoreDualWrite:
    """验证 VaultStore 加密密文 DB 迁移。"""

    async def test_add_item_syncs_to_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import VaultItem as VaultItemORM
        from deadman.vault.store import VaultStore

        monkeypatch.setenv("DEADMAN_VAULT_PASSWORD", "test-vault-password")
        store = VaultStore(data_dir=tmp_path / "vault")

        item = store.add_item(
            owner_user_id="user-v1",
            type="password",
            title="测试密码",
            content="secret-content",
            beneficiary_user_ids=["ben-1"],
            metadata={"account": "test@example.com"},
        )

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            row = await session.get(VaultItemORM, item.item_id)
            assert row is not None
            assert row.owner_user_id == "user-v1"
            assert row.type == "password"
            assert row.title == "测试密码"
            assert row.content_encrypted == item.content_encrypted
            assert row.beneficiary_user_ids == ["ben-1"]
            assert row.item_metadata == {"account": "test@example.com"}
            # 确认 DB 中密文与文件一致（不解密验证）
            assert len(row.content_encrypted) > 0

    async def test_delete_item_removes_from_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import VaultItem as VaultItemORM
        from deadman.vault.store import VaultStore

        monkeypatch.setenv("DEADMAN_VAULT_PASSWORD", "test-vault-password")
        store = VaultStore(data_dir=tmp_path / "vault")

        item = store.add_item(
            owner_user_id="user-v2",
            type="note",
            title="待删除",
            content="will-be-deleted",
            beneficiary_user_ids=[],
        )
        await _await_bg_sync()

        store.delete_item(item.item_id, "user-v2")
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            row = await session.get(VaultItemORM, item.item_id)
            assert row is None


# =====================================================================
# 扩展④h：SwitchStore DB 双写
# =====================================================================

class TestSwitchStoreDualWrite:
    """验证 SwitchStore 加密密文 DB 迁移。"""

    async def test_save_syncs_envelope_to_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import SwitchRecord as SwitchRecordORM
        from deadman.deadman_switch.models import SwitchConfig, SwitchRecord
        from deadman.deadman_switch.store import SwitchStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-switch-pass")
        store = SwitchStore(data_dir=tmp_path / "switch")

        record = SwitchRecord.new("user-s1", SwitchConfig())
        store.save(record)

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            row = await session.get(SwitchRecordORM, "user-s1")
            assert row is not None
            assert row.envelope_text  # 非空加密 envelope
            assert "ct" in row.envelope_text or "nonce" in row.envelope_text

    async def test_record_check_in_syncs_to_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import SwitchCheckIn
        from deadman.db.models import SwitchRecord as SwitchRecordORM
        from deadman.deadman_switch.models import SwitchConfig
        from deadman.deadman_switch.store import SwitchStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-switch-pass")
        store = SwitchStore(data_dir=tmp_path / "switch")
        store.init_switch("user-s2", SwitchConfig())

        store.record_check_in("user-s2", method="web")
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            # check-in 日志
            stmt = select(SwitchCheckIn).where(SwitchCheckIn.user_id == "user-s2")
            checkins = (await session.execute(stmt)).scalars().all()
            assert len(checkins) == 1
            assert checkins[0].method == "web"
            # switch 记录（save 也应同步）
            row = await session.get(SwitchRecordORM, "user-s2")
            assert row is not None

    async def test_delete_removes_from_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import SwitchCheckIn
        from deadman.db.models import SwitchRecord as SwitchRecordORM
        from deadman.deadman_switch.models import SwitchConfig
        from deadman.deadman_switch.store import SwitchStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-switch-pass")
        store = SwitchStore(data_dir=tmp_path / "switch")
        store.init_switch("user-s3", SwitchConfig())
        store.record_check_in("user-s3")
        await _await_bg_sync()

        store.delete("user-s3")
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            row = await session.get(SwitchRecordORM, "user-s3")
            assert row is None
            stmt = select(SwitchCheckIn).where(SwitchCheckIn.user_id == "user-s3")
            checkins = (await session.execute(stmt)).scalars().all()
            assert len(checkins) == 0


# =====================================================================
# 扩展④i：EndingNoteStore DB 双写
# =====================================================================

class TestEndingNoteStoreDualWrite:
    """验证 EndingNoteStore 加密密文 DB 迁移。"""

    async def test_save_syncs_envelope_to_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import EndingNoteRecord
        from deadman.ending_note.models import EndingNote
        from deadman.ending_note.store import EndingNoteStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-note-pass")
        store = EndingNoteStore(data_dir=tmp_path / "notes")

        note = EndingNote.new("user-e1")
        note.personal_info = {"full_name_masked": "测试"}
        store.save(note)

        await _await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            row = await session.get(EndingNoteRecord, "user-e1")
            assert row is not None
            assert row.envelope_text  # 非空加密 envelope

    async def test_share_with_syncs_to_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import EndingNoteIncoming, EndingNoteShare
        from deadman.ending_note.store import EndingNoteStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-note-pass")
        store = EndingNoteStore(data_dir=tmp_path / "notes")

        store.share_with("user-e2", "user-e3", sections=["personal_info"])
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            share = await session.get(EndingNoteShare, "user-e2:user-e3")
            assert share is not None
            assert share.sections == ["personal_info"]
            incoming = await session.get(EndingNoteIncoming, "user-e3:user-e2")
            assert incoming is not None
            assert incoming.sections == ["personal_info"]

    async def test_unshare_removes_from_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import EndingNoteIncoming, EndingNoteShare
        from deadman.ending_note.store import EndingNoteStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-note-pass")
        store = EndingNoteStore(data_dir=tmp_path / "notes")
        store.share_with("user-e4", "user-e5")
        await _await_bg_sync()

        store.unshare("user-e4", "user-e5")
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            share = await session.get(EndingNoteShare, "user-e4:user-e5")
            assert share is None
            incoming = await session.get(EndingNoteIncoming, "user-e5:user-e4")
            assert incoming is None

    async def test_trigger_death_confirmation_syncs_to_db(
        self, initialized_db, tmp_path, monkeypatch
    ):
        from deadman.db.models import EndingNotePendingDelivery
        from deadman.ending_note.store import EndingNoteStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-note-pass")
        store = EndingNoteStore(data_dir=tmp_path / "notes")

        result = store.trigger_delivery("user-e6", "death_confirmation")
        assert result["pending_days"] == 7
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            stmt = select(EndingNotePendingDelivery).where(
                EndingNotePendingDelivery.owner_user_id == "user-e6",
                EndingNotePendingDelivery.trigger_type == "death_confirmation",
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            assert row is not None
            assert row.status == "pending"
            assert row.deliver_at is not None

    async def test_delete_removes_all_from_db(self, initialized_db, tmp_path, monkeypatch):
        from deadman.db.models import (
            EndingNoteIncoming,
            EndingNotePendingDelivery,
            EndingNoteRecord,
            EndingNoteShare,
        )
        from deadman.ending_note.models import EndingNote
        from deadman.ending_note.store import EndingNoteStore

        monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-note-pass")
        store = EndingNoteStore(data_dir=tmp_path / "notes")
        note = EndingNote.new("user-e7")
        store.save(note)
        store.share_with("user-e7", "user-e8")
        store.trigger_delivery("user-e7", "death_confirmation")
        await _await_bg_sync()

        store.delete("user-e7")
        await _await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            assert await session.get(EndingNoteRecord, "user-e7") is None
            assert await session.get(EndingNoteShare, "user-e7:user-e8") is None
            stmt_p = select(EndingNotePendingDelivery).where(
                EndingNotePendingDelivery.owner_user_id == "user-e7"
            )
            assert (await session.execute(stmt_p)).scalar_one_or_none() is None
            stmt_i = select(EndingNoteIncoming).where(
                EndingNoteIncoming.target_user_id == "user-e7"
            )
            assert (await session.execute(stmt_i)).scalar_one_or_none() is None
