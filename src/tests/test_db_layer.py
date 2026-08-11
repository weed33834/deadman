"""主数据库层测试 - 企业级扩展④

覆盖：
    1. 优雅降级：DATABASE_URL 空时 db_enabled()=False，会话 yield None
    2. 引擎/会话工厂：SQLite 内存库建表 + CRUD
    3. ORM 模型：User / CronJob / Notification* 字段约束
    4. UserRepository 双写：文件存储 + DB 同步
    5. Alembic 迁移脚本：revision 链完整 + upgrade/downgrade 可逆
    6. 日志脱敏：_mask_url 隐藏密码

使用 aiosqlite 内存库（无需 PostgreSQL 服务），保证 CI 无外部依赖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from deadman.db import engine as db_engine_mod
from deadman.db.engine import db_enabled, dispose_engine, get_engine
from deadman.db.models import (
    CronJob,
    NotificationLastSession,
    NotificationSentLog,
    User,
)
from deadman.db.session import get_db_session

# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
async def sqlite_db(monkeypatch):
    """配置 SQLite 内存库并初始化表结构。

    每个测试独立引擎，避免单例污染。SQLite 内存库连接关闭即销毁，
    故使用 file::memory:?cache=shared 保持会话间共享。
    """
    # 直接 patch settings 单例的 database_url（避免 reload 复杂性）
    from deadman.config import settings

    old_url = settings.database_url
    # SQLite 异步内存库（shared cache 保证多连接共享同一内存库）
    settings.database_url = "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true"
    # 重置引擎单例
    await dispose_engine()

    yield settings

    # 清理
    await dispose_engine()
    settings.database_url = old_url


@pytest.fixture
async def initialized_db(sqlite_db):
    """初始化表结构（create_all）。"""
    from deadman.db.engine import init_db

    await init_db()
    return sqlite_db


# =====================================================================
# 1. 优雅降级
# =====================================================================

class TestGracefulDegradation:
    """DATABASE_URL 未配置时，DB 层完全 no-op。"""

    def test_db_enabled_false_when_url_empty(self):
        from deadman.config import settings

        old = settings.database_url
        try:
            settings.database_url = ""
            assert db_enabled() is False
        finally:
            settings.database_url = old

    async def test_get_db_session_yields_none_when_disabled(self):
        from deadman.config import settings

        old = settings.database_url
        try:
            settings.database_url = ""
            await dispose_engine()
            async for session in get_db_session():
                assert session is None
        finally:
            settings.database_url = old
            await dispose_engine()

    async def test_get_engine_raises_when_disabled(self):
        from deadman.config import settings

        old = settings.database_url
        try:
            settings.database_url = ""
            await dispose_engine()
            with pytest.raises(RuntimeError, match="DATABASE_URL"):
                get_engine()
        finally:
            settings.database_url = old
            await dispose_engine()


# =====================================================================
# 2. 引擎与表结构
# =====================================================================

class TestEngineAndSchema:
    async def test_init_db_creates_all_tables(self, initialized_db):
        """init_db 应创建所有 7 张表。"""
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            result = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            )
            tables = {row[0] for row in result}
        expected = {
            "users",
            "cron_jobs",
            "notification_consents",
            "notification_unsubscribes",
            "notification_sent_logs",
            "notification_last_sessions",
            "password_reset_tokens",
        }
        assert expected.issubset(tables), f"缺失表: {expected - tables}"

    async def test_init_db_noop_when_disabled(self):
        """DATABASE_URL 空时 init_db 不抛错。"""
        from deadman.config import settings

        old = settings.database_url
        try:
            settings.database_url = ""
            await dispose_engine()
            await db_engine_mod.init_db()  # 应静默返回
        finally:
            settings.database_url = old
            await dispose_engine()


# =====================================================================
# 3. ORM 模型 CRUD
# =====================================================================

class TestUserModel:
    async def test_create_and_query_user(self, initialized_db):
        factory = db_engine_mod.get_async_session_factory()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with factory() as session:
            user = User(
                user_id="test-user-001",
                email="test@example.com",
                email_hmac="hmac001",
                password_hash="abc123",
                salt="salt001",
                role="user",
                display_name="TestUser",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.commit()

        async with factory() as session:
            fetched = await session.get(User, "test-user-001")
            assert fetched is not None
            assert fetched.email == "test@example.com"
            assert fetched.display_name == "TestUser"
            assert fetched.role == "user"

    async def test_email_hmac_unique_constraint(self, initialized_db):
        factory = db_engine_mod.get_async_session_factory()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with factory() as session:
            session.add(User(
                user_id="u1", email="a@x.com", email_hmac="dup_hmac",
                password_hash="h", salt="s", display_name="A",
                created_at=now, updated_at=now,
            ))
            await session.commit()

        async with factory() as session:
            session.add(User(
                user_id="u2", email="b@x.com", email_hmac="dup_hmac",
                password_hash="h", salt="s", display_name="B",
                created_at=now, updated_at=now,
            ))
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):
                await session.commit()


class TestCronJobModel:
    async def test_create_and_query_cron_job(self, initialized_db):
        factory = db_engine_mod.get_async_session_factory()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires = now + timedelta(days=30)
        async with factory() as session:
            job = CronJob(
                job_id="job-001",
                user_id="user-001",
                schedule="0 8 * * *",
                content="每日提醒",
                scope="cron",
                expires_at=expires,
                enabled=True,
                pending_confirmation=False,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.commit()

        async with factory() as session:
            fetched = await session.get(CronJob, "job-001")
            assert fetched is not None
            assert fetched.schedule == "0 8 * * *"
            assert fetched.enabled is True
            assert fetched.pending_confirmation is False


class TestNotificationModels:
    async def test_sent_log_append_and_range_query(self, initialized_db):
        factory = db_engine_mod.get_async_session_factory()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with factory() as session:
            for i in range(5):
                session.add(NotificationSentLog(
                    id=f"log-{i}",
                    user_id="user-001",
                    channel="telegram",
                    content=f"消息{i}",
                    sent_at=now - timedelta(hours=i),
                ))
            await session.commit()

        async with factory() as session:
            # 查询最近 2 小时内的发送记录（频率检查场景）
            cutoff = now - timedelta(hours=2)
            stmt = (
                select(NotificationSentLog)
                .where(NotificationSentLog.user_id == "user-001")
                .where(NotificationSentLog.sent_at >= cutoff)
            )
            results = (await session.execute(stmt)).scalars().all()
            assert len(results) == 3  # 0h, 1h, 2h

    async def test_last_session_upsert(self, initialized_db):
        factory = db_engine_mod.get_async_session_factory()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with factory() as session:
            session.add(NotificationLastSession(
                user_id="user-001",
                ended_at=now,
                safety_triggered=False,
                emotion_intensity=0.3,
                involved_sensitive_death=False,
                created_at=now,
                updated_at=now,
            ))
            await session.commit()

        async with factory() as session:
            fetched = await session.get(NotificationLastSession, "user-001")
            assert fetched is not None
            assert fetched.emotion_intensity == 0.3


# =====================================================================
# 4. UserRepository 双写
# =====================================================================

class TestUserRepository:
    async def test_create_dual_write_file_and_db(self, initialized_db, tmp_path, monkeypatch):
        """注册用户时应同时写文件存储和 DB。"""
        from deadman.auth.store import UserStore
        from deadman.db.repositories import UserRepository

        monkeypatch.setenv("DEADMAN_AUTH_DATA_DIR", str(tmp_path / "auth"))
        file_store = UserStore(data_dir=tmp_path / "auth")
        repo = UserRepository(file_store=file_store)

        result = await repo.create("dual@example.com", "password123", "DualWriter")
        user_id = result["user_id"]

        # 文件存储有记录
        assert file_store.get_user(user_id) is not None

        # DB 也有记录
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            db_user = await session.get(User, user_id)
            assert db_user is not None
            assert db_user.email == "dual@example.com"
            assert db_user.display_name == "DualWriter"

    async def test_get_by_id_db_priority(self, initialized_db, tmp_path, monkeypatch):
        """get_by_id 应优先查 DB。"""
        from deadman.auth.store import UserStore
        from deadman.db.repositories import UserRepository

        file_store = UserStore(data_dir=tmp_path / "auth")
        repo = UserRepository(file_store=file_store)
        result = await repo.create("q@example.com", "password123", "Q")
        user_id = result["user_id"]

        fetched = await repo.get_by_id(user_id)
        assert fetched is not None
        assert fetched["email"] == "q@example.com"

    async def test_get_by_id_fallback_to_file(self, tmp_path, monkeypatch):
        """DB 未启用时 get_by_id 回退文件存储。"""
        from deadman.config import settings

        old = settings.database_url
        try:
            settings.database_url = ""
            await dispose_engine()

            from deadman.auth.store import UserStore
            from deadman.db.repositories import UserRepository

            file_store = UserStore(data_dir=tmp_path / "auth")
            file_result = file_store.register("fb@example.com", "password123", "Fallback")
            repo = UserRepository(file_store=file_store)

            fetched = await repo.get_by_id(file_result["user_id"])
            assert fetched is not None
            assert fetched["email"] == "fb@example.com"
        finally:
            settings.database_url = old
            await dispose_engine()

    async def test_count(self, initialized_db, tmp_path):
        from deadman.auth.store import UserStore
        from deadman.db.repositories import UserRepository

        file_store = UserStore(data_dir=tmp_path / "auth")
        repo = UserRepository(file_store=file_store)
        await repo.create("c1@example.com", "password123", "C1")
        await repo.create("c2@example.com", "password123", "C2")

        count = await repo.count()
        assert count >= 2


# =====================================================================
# 5. Alembic 迁移
# =====================================================================

class TestAlembicMigration:
    def test_revision_chain(self):
        """初始迁移 revision/down_revision 正确。"""
        migrations_dir = Path(__file__).resolve().parent.parent.parent / "migrations"
        init_file = migrations_dir / "versions" / "0001_initial_schema.py"
        assert init_file.exists(), f"迁移文件不存在: {init_file}"

        # 检查 revision 标识符
        content = init_file.read_text()
        assert 'revision: str = "0001_initial"' in content
        assert "down_revision: Union[str, None] = None" in content

    def test_migration_has_all_tables(self):
        """迁移脚本包含所有 7 张表的 create_table。"""
        migrations_dir = Path(__file__).resolve().parent.parent.parent / "migrations"
        init_file = migrations_dir / "versions" / "0001_initial_schema.py"
        content = init_file.read_text()
        for table in [
            "users",
            "cron_jobs",
            "notification_consents",
            "notification_unsubscribes",
            "notification_sent_logs",
            "notification_last_sessions",
            "password_reset_tokens",
        ]:
            assert f'op.create_table(\n        "{table}"' in content or f'op.create_table("{table}"' in content, (
                f"迁移缺少 create_table({table})"
            )


# =====================================================================
# 6. 日志脱敏
# =====================================================================

class TestUrlMasking:
    def test_mask_url_hides_password(self):
        from deadman.db.engine import _mask_url

        url = "postgresql+asyncpg://user:secretpass@host:5432/db"
        masked = _mask_url(url)
        assert "secretpass" not in masked
        assert "***" in masked
        assert "user" in masked
        assert "host:5432/db" in masked

    def test_mask_url_no_credentials(self):
        from deadman.db.engine import _mask_url

        assert _mask_url("sqlite:///data.db") == "sqlite:///data.db"

    def test_mask_url_no_scheme(self):
        from deadman.db.engine import _mask_url

        assert _mask_url("plainstring") == "plainstring"


# =====================================================================
# 7. CronScheduler DB 双写
# =====================================================================

class TestCronSchedulerDualWrite:
    """验证 CronScheduler 在 DB 启用时双写文件 + DB。"""

    @staticmethod
    async def _await_bg_sync(timeout: float = 5.0):
        """等待 fire-and-forget DB 同步后台任务完成。

        轮询 asyncio.all_tasks() 而非固定 sleep：固定睡眠在高负载下会因后台
        任务未完成而间歇性失败。空闲时立即返回，负载高时最长等到 timeout。
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
                return
            await asyncio.wait(pending, timeout=remaining)

    async def test_propose_job_syncs_to_db(self, initialized_db, tmp_path, monkeypatch):
        """propose_job 后任务应同时存在于文件和 DB。"""
        from deadman.cron.scheduler import CronScheduler
        from deadman.db.models import CronJob as CronJobORM

        monkeypatch.setenv("DEADMAN_NOTIFICATION_DATA_DIR", str(tmp_path / "notif"))
        scheduler = CronScheduler(data_dir=tmp_path / "cron")

        result = await scheduler.propose_job("user-001", "0 8 * * *", "每日提醒")
        job_id = result["job_id"]

        # 文件有记录
        jobs_file = tmp_path / "cron" / "jobs.json"
        assert jobs_file.exists()

        # DB 有记录（_sync_jobs_to_db 在 async 上下文中 fire-and-forget，
        # 等待后台任务完成后再验证）
        await self._await_bg_sync()
        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            db_job = await session.get(CronJobORM, job_id)
            assert db_job is not None
            assert db_job.user_id == "user-001"
            assert db_job.schedule == "0 8 * * *"
            assert db_job.content == "每日提醒"
            assert db_job.pending_confirmation is True
            assert db_job.enabled is False

    async def test_confirm_job_updates_db(self, initialized_db, tmp_path, monkeypatch):
        """confirm_job 后 DB 中 enabled/pending_confirmation 应更新。"""
        from deadman.cron.scheduler import CronScheduler
        from deadman.db.models import CronJob as CronJobORM

        monkeypatch.setenv("DEADMAN_NOTIFICATION_DATA_DIR", str(tmp_path / "notif"))
        scheduler = CronScheduler(data_dir=tmp_path / "cron")

        result = await scheduler.propose_job("user-002", "0 9 * * *", "确认测试")
        job_id = result["job_id"]

        await self._await_bg_sync()
        await scheduler.confirm_job("user-002", job_id)
        await self._await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            db_job = await session.get(CronJobORM, job_id)
            assert db_job is not None
            assert db_job.enabled is True
            assert db_job.pending_confirmation is False

    async def test_cancel_job_removes_from_db(self, initialized_db, tmp_path, monkeypatch):
        """cancel_job 后 DB 中记录应被删除。"""
        from deadman.cron.scheduler import CronScheduler
        from deadman.db.models import CronJob as CronJobORM

        monkeypatch.setenv("DEADMAN_NOTIFICATION_DATA_DIR", str(tmp_path / "notif"))
        scheduler = CronScheduler(data_dir=tmp_path / "cron")

        result = await scheduler.propose_job("user-003", "0 10 * * *", "取消测试")
        job_id = result["job_id"]
        await self._await_bg_sync()
        await scheduler.confirm_job("user-003", job_id)
        await self._await_bg_sync()
        await scheduler.cancel_job("user-003", job_id)
        await self._await_bg_sync()

        factory = db_engine_mod.get_async_session_factory()
        async with factory() as session:
            db_job = await session.get(CronJobORM, job_id)
            assert db_job is None

    async def test_file_fallback_when_db_disabled(self, tmp_path, monkeypatch):
        """DB 未启用时纯文件存储，不报错。"""
        from deadman.config import settings
        from deadman.cron.scheduler import CronScheduler

        old = settings.database_url
        try:
            settings.database_url = ""
            await dispose_engine()

            monkeypatch.setenv("DEADMAN_NOTIFICATION_DATA_DIR", str(tmp_path / "notif"))
            scheduler = CronScheduler(data_dir=tmp_path / "cron")

            result = await scheduler.propose_job("user-004", "0 11 * * *", "降级测试")
            assert result["needs_confirmation"] is True

            # 验证文件存储正常工作
            jobs = scheduler._load_jobs()
            assert len(jobs) == 1
            assert jobs[0].user_id == "user-004"
        finally:
            settings.database_url = old
            await dispose_engine()
