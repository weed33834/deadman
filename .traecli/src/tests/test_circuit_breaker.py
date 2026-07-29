"""P7.2 熔断器(Circuit Breaker)测试。"""

from __future__ import annotations

import time

import pytest

from deadman.infrastructure.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerRegistry,
    CircuitConfig,
    CircuitState,
)


@pytest.fixture(autouse=True)
def enable_circuit_breaker(monkeypatch):
    """强制启用熔断器 feature flag(测试默认关闭,需要打开)。"""
    monkeypatch.setenv("DEADMAN_CIRCUIT_BREAKER_ENABLED", "1")
    # 清缓存
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


class TestClosedState:
    """Closed 状态行为。"""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("test_cb")
        assert cb.state == CircuitState.CLOSED

    def test_acquire_returns_token_when_closed(self):
        cb = CircuitBreaker("test_cb")
        token = cb.acquire()
        assert token is not None
        assert isinstance(token, str)

    def test_success_does_not_open_circuit(self):
        cb = CircuitBreaker("test_cb", config=CircuitConfig(minimum_number_of_calls=5))
        for _ in range(10):
            cb.acquire()
            cb.release_success(duration=0.01)
        assert cb.state == CircuitState.CLOSED


class TestOpenTransition:
    """Closed → Open 转换。"""

    def test_high_failure_rate_opens_circuit(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=10,
                sliding_window_size=100,
            ),
        )
        # 10 次调用,6 次失败(60% > 50%)
        for i in range(10):
            cb.acquire()
            if i < 6:
                cb.release_failure(error=Exception("boom"))
            else:
                cb.release_success()
        assert cb.state == CircuitState.OPEN

    def test_low_failure_rate_keeps_closed(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(failure_rate_threshold=0.5, minimum_number_of_calls=10),
        )
        # 10 次调用,3 次失败(30% < 50%)
        for i in range(10):
            cb.acquire()
            if i < 3:
                cb.release_failure(error=Exception("boom"))
            else:
                cb.release_success()
        assert cb.state == CircuitState.CLOSED

    def test_below_min_calls_does_not_open(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(failure_rate_threshold=0.5, minimum_number_of_calls=10),
        )
        # 只 5 次全失败,不够 minimum_number_of_calls
        for _ in range(5):
            cb.acquire()
            cb.release_failure(error=Exception("boom"))
        assert cb.state == CircuitState.CLOSED

    def test_open_state_raises_exception(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(failure_rate_threshold=0.5, minimum_number_of_calls=5),
        )
        for _ in range(5):
            cb.acquire()
            cb.release_failure(error=Exception("boom"))
        # 现在 Open,acquire 应抛异常
        with pytest.raises(CircuitBreakerOpenError):
            cb.acquire()


class TestOpenToHalfOpen:
    """Open → Half-Open 转换。"""

    def test_open_transitions_to_half_open_after_cooldown(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=1,  # 短冷却便于测试
            ),
        )
        # 触发 Open
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        assert cb.state == CircuitState.OPEN

        # 等待冷却结束
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN

    def test_open_stays_open_during_cooldown(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=10,
            ),
        )
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        time.sleep(0.1)
        # 还在冷却中
        assert cb.state == CircuitState.OPEN


class TestHalfOpen:
    """Half-Open 试探。"""

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=1,
                permitted_number_of_calls_in_half_open=3,
            ),
        )
        # 触发 Open
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN

        # 试探 3 次全部成功 → Closed
        for _ in range(3):
            cb.acquire()
            cb.release_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=1,
                permitted_number_of_calls_in_half_open=3,
            ),
        )
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        time.sleep(1.1)

        # 试探 3 次全部失败 → 重新 Open
        for _ in range(3):
            cb.acquire()
            cb.release_failure()
        assert cb.state == CircuitState.OPEN

    def test_half_open_trial_limit(self):
        """Half-Open 状态下试探名额满了拒绝新请求。"""
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=1,
                permitted_number_of_calls_in_half_open=2,
            ),
        )
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        time.sleep(1.1)
        # 占满试探名额
        cb.acquire()
        cb.acquire()
        # 第 3 个应被拒绝
        with pytest.raises(CircuitBreakerOpenError):
            cb.acquire()


class TestBackoff:
    """指数退避(Open 重新触发时翻倍)。"""

    def test_backoff_multiplier_increases_on_failure(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=1,
                permitted_number_of_calls_in_half_open=3,
                max_backoff_multiplier=8,
            ),
        )
        # 第一次 Open(首次保持 backoff = 1)
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        assert cb._backoff_multiplier == 1  # 首次 Open 不翻倍

        time.sleep(1.1)  # 等 1s(cooldown=1 * backoff=1)
        # 试探失败 → 重新 Open,倍数翻倍到 2
        for _ in range(3):
            cb.acquire()
            cb.release_failure()
        assert cb._backoff_multiplier == 2

    def test_backoff_capped_at_max(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(
                failure_rate_threshold=0.5,
                minimum_number_of_calls=5,
                wait_duration_in_open_state_seconds=0,
                permitted_number_of_calls_in_half_open=3,
                max_backoff_multiplier=4,
            ),
        )
        # 直接设置 backoff 测试封顶
        cb._backoff_multiplier = 4
        # 模拟 Half-Open 失败 → 重新 Open,倍数应封顶在 4
        cb._state = CircuitState.HALF_OPEN
        cb._half_open_success = 0
        cb._half_open_failure = 3
        cb._half_open_trials = 3
        cb._check_half_open_completion()
        # 4 * 2 = 8,但被 max_backoff_multiplier=4 封顶
        assert cb._backoff_multiplier == 4


class TestMetrics:
    """get_metrics(看板)。"""

    def test_metrics_includes_state_and_counts(self):
        cb = CircuitBreaker("test_cb")
        for _ in range(5):
            cb.acquire()
            cb.release_success()
        m = cb.get_metrics()
        assert m["state"] == "closed"
        assert m["total_calls"] == 5
        assert m["failures"] == 0
        assert m["failure_rate"] == 0.0

    def test_reset_clears_state(self):
        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(failure_rate_threshold=0.5, minimum_number_of_calls=5),
        )
        for _ in range(5):
            cb.acquire()
            cb.release_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._backoff_multiplier == 1


class TestRegistry:
    """CircuitBreakerRegistry。"""

    def test_get_or_create_returns_same_instance(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("llm_openai")
        cb2 = reg.get_or_create("llm_openai")
        assert cb1 is cb2

    def test_list_all(self):
        reg = CircuitBreakerRegistry()
        reg.get_or_create("llm_openai")
        reg.get_or_create("llm_anthropic")
        all_cbs = reg.list_all()
        names = {cb.name for cb in all_cbs}
        assert "llm_openai" in names
        assert "llm_anthropic" in names

    def test_reset_all(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("cb1")
        cb2 = reg.get_or_create("cb2")
        cb1._state = CircuitState.OPEN
        cb2._state = CircuitState.OPEN
        reg.reset_all()
        assert cb1.state == CircuitState.CLOSED
        assert cb2.state == CircuitState.CLOSED


class TestFeatureFlagBypass:
    """feature flag 关闭时完全透传。"""

    def test_disabled_flag_bypasses_circuit(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_CIRCUIT_BREAKER_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        cb = CircuitBreaker(
            "test_cb",
            config=CircuitConfig(failure_rate_threshold=0.5, minimum_number_of_calls=5),
        )
        # 即使全部失败,flag 关闭时也不应打开熔断器
        for _ in range(20):
            cb.acquire()
            cb.release_failure()
        # acquire 仍能通过(不抛异常)
        cb.acquire()
