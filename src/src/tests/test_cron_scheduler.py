"""测试 deadman.cron.scheduler - Cron 调度器

覆盖点（12 个）：
  - test_propose_needs_confirmation
  - test_confirm_activates_job
  - test_confirm_without_propose_fails
  - test_max_jobs_per_user
  - test_min_interval_rejected
  - test_max_duration_expires
  - test_tick_skips_unconfirmed
  - test_tick_skips_guardrail_blocked
  - test_tick_skips_sanitized_empty
  - test_tick_fires_valid_job
  - test_tick_failure_no_retry
  - test_cancel_job

严格遵守 notification-guardrails.md 第三章约束。
NotificationGuardrail 用 MagicMock 注入（不依赖 Phase 3 实际实现）。
不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from deadman.cron.scheduler import CronJob, CronScheduler

# =====================================================================
# 辅助：构造 mock guard / scheduler
# =====================================================================


def _make_mock_guard(
    can_send: tuple[bool, str] = (True, ""),
    sanitize_map: dict[str, str] | None = None,
) -> MagicMock:
    """构造一个 mock NotificationGuardrail

    Args:
        can_send: can_send 默认返回值 (allowed, reason)
        sanitize_map: content → sanitized 的映射；未命中的原样返回。
            用于测试 "忌日" 被脱敏为空串的场景。
    """
    guard = MagicMock()

    guard.can_send.return_value = can_send

    if sanitize_map is None:
        guard.sanitize_content.side_effect = lambda content: content
    else:
        guard.sanitize_content.side_effect = lambda content: sanitize_map.get(content, content)

    guard.record_consent.return_value = None
    guard.record_send.return_value = None
    guard.record_unsubscribe.return_value = None
    return guard


def _make_scheduler(
    tmp_path,
    guard: MagicMock | None = None,
    fire_handler=None,
) -> CronScheduler:
    """构造一个用 tmp_path 数据目录的 CronScheduler（隔离测试）"""
    if guard is None:
        guard = _make_mock_guard()
    if fire_handler is None:
        fire_handler = AsyncMock()
    return CronScheduler(
        data_dir=tmp_path / "cron",
        guard=guard,
        fire_handler=fire_handler,
    )


# =====================================================================
# propose / confirm 生命周期
# =====================================================================


class TestProposeConfirm:
    """propose / confirm 双重确认机制"""

    def test_propose_needs_confirmation(self, tmp_path):
        """propose 后任务 pending_confirmation=True, enabled=False"""
        sched = _make_scheduler(tmp_path)
        result = asyncio.run(
            sched.propose_job(
                user_id="u1",
                schedule="0 9 * * *",
                content="提醒办户籍事务",
            )
        )
        assert result["needs_confirmation"] is True
        assert "job_id" in result

        jobs = sched.list_jobs("u1")
        assert len(jobs) == 1
        assert jobs[0].pending_confirmation is True
        assert jobs[0].enabled is False
        assert jobs[0].schedule == "0 9 * * *"

    def test_confirm_activates_job(self, tmp_path):
        """confirm 后 pending_confirmation=False, enabled=True"""
        sched = _make_scheduler(tmp_path)
        propose_result = asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒办户籍事务"))
        job_id = propose_result["job_id"]

        confirm_result = asyncio.run(sched.confirm_job("u1", job_id))
        assert confirm_result["confirmed"] is True

        jobs = sched.list_jobs("u1")
        assert len(jobs) == 1
        assert jobs[0].pending_confirmation is False
        assert jobs[0].enabled is True

    def test_confirm_without_propose_fails(self, tmp_path):
        """直接 confirm 不存在的 job_id 报错"""
        sched = _make_scheduler(tmp_path)
        with pytest.raises(ValueError, match="未找到"):
            asyncio.run(sched.confirm_job("u1", "nonexistent-job-id"))


# =====================================================================
# 任务粒度约束
# =====================================================================


class TestJobLimits:
    """单用户上限 / 最小间隔 / 最长持续"""

    def test_max_jobs_per_user(self, tmp_path):
        """第 6 个任务被拒（上限 5 条/用户）"""
        sched = _make_scheduler(tmp_path)
        for i in range(5):
            r = asyncio.run(sched.propose_job("u1", "0 9 * * *", f"提醒事项 {i}"))
            asyncio.run(sched.confirm_job("u1", r["job_id"]))

        # 第 6 个：propose 能成功（propose 不校验上限，避免误占名额），
        # 但 confirm 应该被拒
        r6 = asyncio.run(sched.propose_job("u1", "0 10 * * *", "第 6 个提醒"))
        with pytest.raises(ValueError, match="超过上限"):
            asyncio.run(sched.confirm_job("u1", r6["job_id"]))

    def test_min_interval_rejected(self, tmp_path):
        """ "0 * * * *"（每小时，间隔 1h < 24h）confirm 时被拒"""
        sched = _make_scheduler(tmp_path)
        r = asyncio.run(sched.propose_job("u1", "0 * * * *", "每小时提醒"))
        with pytest.raises(ValueError, match="间隔"):
            asyncio.run(sched.confirm_job("u1", r["job_id"]))

    def test_max_duration_expires(self, tmp_path):
        """31 天后任务自动失效（expires_at < now）"""
        sched = _make_scheduler(tmp_path)
        r = asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒事项"))
        asyncio.run(sched.confirm_job("u1", r["job_id"]))

        # confirm 后 expires_at 应 ≤ now + 30 天
        jobs = sched.list_jobs("u1")
        now = datetime.now()
        assert jobs[0].expires_at <= now + timedelta(days=30) + timedelta(seconds=5)

        # 31 天后 tick，任务应因 expired 被跳过
        future = now + timedelta(days=31)
        results = asyncio.run(sched.tick(now=future))
        assert len(results) == 1
        assert results[0]["fired"] is False
        assert results[0]["reason"] == "expired"


# =====================================================================
# tick 触发逻辑
# =====================================================================


class TestTick:
    """tick 调度逻辑测试"""

    def test_tick_skips_unconfirmed(self, tmp_path):
        """未确认任务不触发（pending_confirmation=True）"""
        sched = _make_scheduler(tmp_path)
        asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒事项"))
        # 不 confirm，直接 tick
        results = asyncio.run(sched.tick(now=datetime(2026, 7, 21, 9, 0)))
        assert len(results) == 1
        assert results[0]["fired"] is False
        assert results[0]["reason"] == "pending_confirmation"

    def test_tick_skips_guardrail_blocked(self, tmp_path):
        """guard.can_send 返回 False 时任务不触发"""
        guard = _make_mock_guard(can_send=(False, "silent_hours"))
        sched = _make_scheduler(tmp_path, guard=guard)
        r = asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒事项"))
        asyncio.run(sched.confirm_job("u1", r["job_id"]))

        results = asyncio.run(sched.tick(now=datetime(2026, 7, 21, 9, 0)))
        assert len(results) == 1
        assert results[0]["fired"] is False
        assert results[0]["reason"].startswith("guard_blocked")
        assert "silent_hours" in results[0]["reason"]

    def test_tick_skips_sanitized_empty(self, tmp_path):
        """content 含 "忌日" 被 sanitize 为空串，任务不触发

        notification-guardrails.md §二.5: 忌日/周年 → 完全不推送
        """
        guard = _make_mock_guard(
            can_send=(True, ""),
            sanitize_map={"今天是忌日": ""},
        )
        sched = _make_scheduler(tmp_path, guard=guard)
        r = asyncio.run(sched.propose_job("u1", "0 9 * * *", "今天是忌日"))
        asyncio.run(sched.confirm_job("u1", r["job_id"]))

        results = asyncio.run(sched.tick(now=datetime(2026, 7, 21, 9, 0)))
        assert len(results) == 1
        assert results[0]["fired"] is False
        assert results[0]["reason"] == "sanitized_empty"

    def test_tick_fires_valid_job(self, tmp_path):
        """全部通过的任务触发"""
        fire_handler = AsyncMock()
        sched = _make_scheduler(tmp_path, fire_handler=fire_handler)
        r = asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒办户籍事务"))
        asyncio.run(sched.confirm_job("u1", r["job_id"]))

        results = asyncio.run(sched.tick(now=datetime(2026, 7, 21, 9, 0)))
        assert len(results) == 1
        assert results[0]["fired"] is True
        assert results[0]["reason"] == "fired"

        # fire_handler 被调用一次，参数为 (job, sanitized_content)
        fire_handler.assert_called_once()
        call_args = fire_handler.call_args
        job_arg = call_args.args[0]
        content_arg = call_args.args[1]
        assert job_arg.job_id == r["job_id"]
        assert content_arg == "提醒办户籍事务"

        # last_fired 已更新
        jobs = sched.list_jobs("u1")
        assert jobs[0].last_fired is not None

    def test_tick_failure_no_retry(self, tmp_path):
        """触发失败后 last_fired 更新但不重试

        notification-guardrails.md §三.4: 失败不自动重试，记录日志，
        下次用户主动对话时报告"昨天的提醒发送失败"。
        """
        fire_handler = AsyncMock(side_effect=RuntimeError("模拟推送失败"))
        sched = _make_scheduler(tmp_path, fire_handler=fire_handler)
        r = asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒事项"))
        asyncio.run(sched.confirm_job("u1", r["job_id"]))

        # 第一次 tick：触发失败
        t = datetime(2026, 7, 21, 9, 0)
        results1 = asyncio.run(sched.tick(now=t))
        assert len(results1) == 1
        assert results1[0]["fired"] is False
        assert "fire_failed" in results1[0]["reason"]
        assert fire_handler.call_count == 1

        # last_fired 已更新（失败也更新，防本分钟重试）
        jobs = sched.list_jobs("u1")
        assert jobs[0].last_fired is not None

        # 同一分钟的第二次 tick：不应再次调用 fire_handler
        results2 = asyncio.run(sched.tick(now=t))
        assert len(results2) == 1
        assert results2[0]["fired"] is False
        assert results2[0]["reason"] == "already_fired_this_minute"
        # 调用次数仍是 1（没有重试）
        assert fire_handler.call_count == 1


# =====================================================================
# cancel
# =====================================================================


class TestCancel:
    """cancel_job 测试"""

    def test_cancel_job(self, tmp_path):
        """cancel 后任务不再触发"""
        fire_handler = AsyncMock()
        sched = _make_scheduler(tmp_path, fire_handler=fire_handler)
        r = asyncio.run(sched.propose_job("u1", "0 9 * * *", "提醒事项"))
        job_id = r["job_id"]
        asyncio.run(sched.confirm_job("u1", job_id))

        # cancel
        ok = asyncio.run(sched.cancel_job("u1", job_id))
        assert ok is True

        # 任务已从列表移除
        assert sched.list_jobs("u1") == []

        # tick 时该任务不再出现
        results = asyncio.run(sched.tick(now=datetime(2026, 7, 21, 9, 0)))
        assert len(results) == 0
        fire_handler.assert_not_called()

        # 再次 cancel 同一 job_id 应失败（不存在）
        ok2 = asyncio.run(sched.cancel_job("u1", job_id))
        assert ok2 is False


# =====================================================================
# CronJob 序列化往返
# =====================================================================


class TestCronJobSerialization:
    """CronJob 序列化往返一致性（确保 jobs.json 读写不丢字段）"""

    def test_job_roundtrip(self):
        """CronJob.to_dict / from_dict 往返一致"""
        now = datetime(2026, 7, 21, 9, 0, 0)
        job = CronJob(
            job_id="abc123",
            user_id="u1",
            schedule="0 9 * * *",
            content="提醒办户籍事务",
            scope="cron",
            created_at=now,
            expires_at=now + timedelta(days=30),
            last_fired=None,
            enabled=True,
            pending_confirmation=False,
        )
        d = job.to_dict()
        restored = CronJob.from_dict(d)
        assert restored.job_id == job.job_id
        assert restored.user_id == job.user_id
        assert restored.schedule == job.schedule
        assert restored.content == job.content
        assert restored.scope == job.scope
        assert restored.created_at == job.created_at
        assert restored.expires_at == job.expires_at
        assert restored.last_fired == job.last_fired
        assert restored.enabled == job.enabled
        assert restored.pending_confirmation == job.pending_confirmation
