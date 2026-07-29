"""P7.7 配额与计费测试。"""

from __future__ import annotations

import time

import pytest
from deadman.infrastructure.quota import (
    QuotaAction,
    QuotaExceededError,
    QuotaManager,
    SlidingWindowCounter,
)


@pytest.fixture(autouse=True)
def enable_quota(monkeypatch):
    monkeypatch.setenv("DEADMAN_QUOTA_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


class TestSlidingWindowCounter:
    def test_initial_count_is_zero(self):
        c = SlidingWindowCounter(window_seconds=60)
        assert c.current() == 0

    def test_add_increments(self):
        c = SlidingWindowCounter(window_seconds=60)
        c.add(1)
        assert c.current() == 1
        c.add(5)
        assert c.current() == 6

    def test_old_buckets_evicted(self):
        """超出窗口的 bucket 应被清除。"""
        clock_time = [1000.0]
        c = SlidingWindowCounter(window_seconds=60, bucket_count=6)
        # 用自定义 clock 模拟
        c.add(5, now=clock_time[0])
        assert c.current(now=clock_time[0]) == 5
        # 前进 70 秒(超窗口)
        clock_time[0] += 70
        assert c.current(now=clock_time[0]) == 0

    def test_reset_at_returns_window_end(self):
        c = SlidingWindowCounter(window_seconds=60)
        c.add(1)
        reset_at = c.reset_at()
        # reset_at 应该在 now + 60s 附近
        assert reset_at > time.time()

    def test_persist_and_reload(self):
        c1 = SlidingWindowCounter(window_seconds=60)
        c1.add(10)
        data = c1.to_dict()
        c2 = SlidingWindowCounter.from_dict(data)
        assert c2.current() == 10


class TestConsume:
    def test_consume_under_limit(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        usage = qm.consume("llm_tokens", amount=100, tenant_id="t1")
        assert usage.used == 100
        assert usage.utilization == 100 / 100_000
        assert QuotaAction.REJECT not in usage.triggered_actions

    def test_consume_at_warn_threshold(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        # 默认 warn 阈值 0.8 → 80%
        qm.consume("llm_tokens", amount=80_000, tenant_id="t1")
        usage = qm.consume("llm_tokens", amount=1, tenant_id="t1")
        assert QuotaAction.WARN in usage.triggered_actions

    def test_consume_at_downgrade_threshold(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        # 0.9 → 90_000
        qm.consume("llm_tokens", amount=90_000, tenant_id="t1")
        usage = qm.consume("llm_tokens", amount=1, tenant_id="t1")
        assert QuotaAction.DOWNGRADE_MODEL in usage.triggered_actions

    def test_consume_exceeds_rejects(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        # 100_001 > 100_000 limit
        with pytest.raises(QuotaExceededError):
            qm.consume("llm_tokens", amount=100_001, tenant_id="t1")

    def test_consume_persists_across_instances(self, tmp_path):
        store = tmp_path / "quota.json"
        qm1 = QuotaManager(store_path=store)
        qm1.consume("llm_tokens", amount=50_000, tenant_id="t1")

        qm2 = QuotaManager(store_path=store)
        usage = qm2.check("llm_tokens", tenant_id="t1")
        assert usage.used == 50_000


class TestCheck:
    def test_check_does_not_consume(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        qm.consume("llm_tokens", amount=100, tenant_id="t1")
        # check 不应增加计数
        usage = qm.check("llm_tokens", tenant_id="t1")
        assert usage.used == 100  # 还是 100,不是 101

    def test_check_returns_remaining(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        qm.consume("llm_tokens", amount=30_000, tenant_id="t1")
        usage = qm.check("llm_tokens", tenant_id="t1")
        assert usage.remaining == 70_000


class TestTenantIsolation:
    def test_different_tenants_independent(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        qm.consume("llm_tokens", amount=80_000, tenant_id="t1")
        # t2 应该不受影响
        usage_t2 = qm.check("llm_tokens", tenant_id="t2")
        assert usage_t2.used == 0
        assert QuotaAction.WARN not in usage_t2.triggered_actions


class TestTenantOverride:
    def test_set_tenant_limit_overrides_default(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        # t1 设为 200_000
        qm.set_tenant_limit("t1", "llm_tokens", limit=200_000)
        # 100_001 不超过 t1 的 200_000,但超过默认 100_000
        usage = qm.consume("llm_tokens", amount=100_001, tenant_id="t1")
        assert QuotaAction.REJECT not in usage.triggered_actions


class TestReset:
    def test_reset_all(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        qm.consume("llm_tokens", amount=50_000, tenant_id="t1")
        qm.consume("tool_calls", amount=500, tenant_id="t1")
        qm.reset()
        assert qm.check("llm_tokens", tenant_id="t1").used == 0

    def test_reset_tenant(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        qm.consume("llm_tokens", amount=50_000, tenant_id="t1")
        qm.consume("llm_tokens", amount=50_000, tenant_id="t2")
        qm.reset(tenant_id="t1")
        assert qm.check("llm_tokens", tenant_id="t1").used == 0
        assert qm.check("llm_tokens", tenant_id="t2").used == 50_000

    def test_reset_single_quota(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        qm.consume("llm_tokens", amount=50_000, tenant_id="t1")
        qm.consume("tool_calls", amount=500, tenant_id="t1")
        qm.reset(tenant_id="t1", quota_name="llm_tokens")
        assert qm.check("llm_tokens", tenant_id="t1").used == 0
        assert qm.check("tool_calls", tenant_id="t1").used == 500


class TestListUsage:
    def test_list_returns_all_quotas(self, tmp_path):
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        usages = qm.list_usage(tenant_id="t1")
        names = {u.name for u in usages}
        assert "llm_tokens" in names
        assert "tool_calls" in names
        assert "api_requests" in names


class TestFeatureFlagDisabled:
    def test_disabled_returns_infinite_quota(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_QUOTA_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        qm = QuotaManager(store_path=tmp_path / "quota.json")
        # 即使消费 100 亿也不应抛异常
        usage = qm.consume("llm_tokens", amount=10_000_000_000, tenant_id="t1")
        assert usage.utilization == 0.0  # 1B << 1B limit
