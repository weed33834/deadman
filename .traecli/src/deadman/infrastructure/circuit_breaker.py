"""P7.2 熔断器(Circuit Breaker) - 三态机 Closed/Open/Half-Open。

借鉴 Netflix Hystrix + Resilience4j 的成熟模式,适配 LLM 调用 / 工具调用场景。

状态机:
    Closed  --(失败率 > 阈值)-->  Open
    Open    --(冷却时间结束)-->   Half-Open
    Half-Open --(试探成功)-->    Closed
    Half-Open --(试探失败)-->    Open(冷却时间翻倍,指数退避)

关键参数(均有合理默认值,可按场景覆盖):
    - failure_rate_threshold: 0.5  触发 Open 的失败率
    - slow_call_rate_threshold: 1.0  慢调用占比阈值(>1s 视为慢调用)
    - minimum_number_of_calls: 10   滑动窗口最少 N 次才评估
    - wait_duration_in_open_state: 60s  Open 状态最短持续时间
    - permitted_number_of_calls_in_half_open: 3  Half-Open 试探次数
    - sliding_window_size: 100  滑动窗口大小
    - max_backoff_multiplier: 8  指数退避最大倍数

并发安全:所有状态变更加锁。
集成点:LLM 调用 / 工具调用 / A2A 调用前调用 cb.acquire() / cb.release_success() / cb.release_failure()。

feature flag:`DEADMAN_CIRCUIT_BREAKER_ENABLED=0` 默认关闭。
关闭时 acquire() 直接返回 success,完全透传。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .feature_flags import is_enabled

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    """熔断器三态(借鉴 Resilience4j)。"""

    CLOSED = "closed"  # 正常放行,统计失败率
    OPEN = "open"  # 熔断中,直接拒绝(快速失败)
    HALF_OPEN = "half_open"  # 半开,允许 N 个试探请求


class CircuitBreakerOpenError(Exception):
    """熔断器 Open 状态时抛出,调用方应快速失败或降级。"""

    def __init__(self, name: str, retry_after_seconds: float) -> None:
        self.name = name
        self.retry_after = retry_after_seconds
        super().__init__(
            f"Circuit breaker '{name}' is OPEN. Retry after {retry_after_seconds:.1f}s."
        )


@dataclass
class CircuitConfig:
    """熔断器配置(每个 circuit 独立配置)。"""

    failure_rate_threshold: float = 0.5  # 失败率阈值 0-1
    slow_call_rate_threshold: float = 1.0  # 慢调用率阈值 0-1
    slow_call_duration_threshold_seconds: float = 1.0  # 慢调用判定阈值
    minimum_number_of_calls: int = 10  # 窗口内最少 N 次才评估
    wait_duration_in_open_state_seconds: int = 60  # Open 最短持续时间
    permitted_number_of_calls_in_half_open: int = 3  # Half-Open 试探次数
    sliding_window_size: int = 100  # 滑动窗口大小
    max_backoff_multiplier: int = 8  # 指数退避最大倍数


@dataclass
class _CallRecord:
    """单次调用记录(用于滑动窗口统计)。"""

    success: bool
    duration: float
    timestamp: float


class CircuitBreaker:
    """单实例熔断器。

    典型用法:
        cb = CircuitBreaker("llm_openai", config=CircuitConfig(failure_rate_threshold=0.5))
        try:
            token = cb.acquire()  # Open 状态会抛 CircuitBreakerOpenError
            result = await llm.chat(...)
            cb.release_success(duration=time.time()-start)
        except CircuitBreakerOpenError:
            # 降级到 fallback
            result = "..."
        except Exception as e:
            cb.release_failure(duration=time.time()-start, error=e)
            raise

    并发安全:RLock 保护状态变更 + 滑动窗口。
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitConfig] = None,
        feature_flag_name: str = "circuit_breaker",
    ) -> None:
        self.name = name
        self.config = config or CircuitConfig()
        self.feature_flag_name = feature_flag_name
        self._lock = threading.RLock()
        self._state: CircuitState = CircuitState.CLOSED
        # 滑动窗口(最近 N 次调用记录)
        self._sliding_window: deque[_CallRecord] = deque(maxlen=self.config.sliding_window_size)
        # Open 状态开始时间(用于判断冷却是否结束)
        self._opened_at: float = 0.0
        # 当前 backoff 倍数(Open 重新触发时翻倍,上限 max_backoff_multiplier)
        self._backoff_multiplier: int = 1
        # Half-Open 状态下已发出的试探请求数
        self._half_open_trials: int = 0
        # Half-Open 状态下的试探结果
        self._half_open_success: int = 0
        self._half_open_failure: int = 0

    # ==================================================================
    # 公开 API
    # ==================================================================

    @property
    def state(self) -> CircuitState:
        """当前状态(惰性 Open→Half-Open 转换)。"""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    def acquire(self) -> str:
        """请求通过熔断器。

        Returns:
            token(str) - 调用方需在 release_success/release_failure 时传回(目前仅作 placeholder)

        Raises:
            CircuitBreakerOpenError: 当前处于 Open 状态,应快速失败
        """
        # Feature flag 关闭 → 完全透传
        if not is_enabled(self.feature_flag_name):
            return "bypass"

        with self._lock:
            # 惰性 Open → Half-Open
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.OPEN:
                # 计算剩余冷却时间
                elapsed = time.time() - self._opened_at
                wait_total = self.config.wait_duration_in_open_state_seconds * self._backoff_multiplier
                retry_after = max(0.0, wait_total - elapsed)
                raise CircuitBreakerOpenError(self.name, retry_after)

            if self._state == CircuitState.HALF_OPEN:
                # Half-Open 状态:试探请求并发数限制
                if (
                    self._half_open_trials
                    >= self.config.permitted_number_of_calls_in_half_open
                ):
                    # 试探名额已满,拒绝新请求(等待已有试探完成)
                    raise CircuitBreakerOpenError(self.name, 1.0)
                self._half_open_trials += 1

            # CLOSED 或 HALF_OPEN 允许通过
            return str(time.time())

    def release_success(self, duration: float = 0.0) -> None:
        """记录一次成功调用。"""
        if not is_enabled(self.feature_flag_name):
            return

        with self._lock:
            record = _CallRecord(success=True, duration=duration, timestamp=time.time())
            self._sliding_window.append(record)

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_success += 1
                self._check_half_open_completion()

            elif self._state == CircuitState.CLOSED:
                self._maybe_open_circuit()

    def release_failure(
        self,
        duration: float = 0.0,
        error: Optional[Exception] = None,
    ) -> None:
        """记录一次失败调用。"""
        if not is_enabled(self.feature_flag_name):
            return

        with self._lock:
            record = _CallRecord(success=False, duration=duration, timestamp=time.time())
            self._sliding_window.append(record)

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_failure += 1
                self._check_half_open_completion()

            elif self._state == CircuitState.CLOSED:
                self._maybe_open_circuit()

    def reset(self) -> None:
        """强制重置到 Closed(用于手动恢复或测试)。"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._sliding_window.clear()
            self._opened_at = 0.0
            self._backoff_multiplier = 1
            self._half_open_trials = 0
            self._half_open_success = 0
            self._half_open_failure = 0

    def get_metrics(self) -> dict:
        """获取熔断器指标(看板用)。"""
        with self._lock:
            total = len(self._sliding_window)
            if total == 0:
                return {
                    "name": self.name,
                    "state": self._state.value,
                    "total_calls": 0,
                    "failure_rate": 0.0,
                    "backoff_multiplier": self._backoff_multiplier,
                }
            failures = sum(1 for r in self._sliding_window if not r.success)
            slow_calls = sum(
                1 for r in self._sliding_window
                if r.duration > self.config.slow_call_duration_threshold_seconds
            )
            return {
                "name": self.name,
                "state": self._state.value,
                "total_calls": total,
                "failures": failures,
                "failure_rate": failures / total,
                "slow_calls": slow_calls,
                "slow_call_rate": slow_calls / total,
                "backoff_multiplier": self._backoff_multiplier,
                "opened_at": self._opened_at,
            }

    # ==================================================================
    # 内部:状态转换
    # ==================================================================

    def _maybe_transition_to_half_open(self) -> None:
        """Open 状态冷却结束 → 切换到 Half-Open(允许试探)。"""
        if self._state != CircuitState.OPEN:
            return
        elapsed = time.time() - self._opened_at
        wait_total = self.config.wait_duration_in_open_state_seconds * self._backoff_multiplier
        if elapsed >= wait_total:
            self._state = CircuitState.HALF_OPEN
            self._half_open_trials = 0
            self._half_open_success = 0
            self._half_open_failure = 0
            logger.info(
                "Circuit '%s' OPEN→HALF_OPEN after %.1fs (backoff x%d)",
                self.name,
                elapsed,
                self._backoff_multiplier,
            )

    def _maybe_open_circuit(self) -> None:
        """Closed 状态下检查是否需要切换到 Open。"""
        # 滑动窗口样本不足,不评估
        if len(self._sliding_window) < self.config.minimum_number_of_calls:
            return

        total = len(self._sliding_window)
        failures = sum(1 for r in self._sliding_window if not r.success)
        failure_rate = failures / total
        slow_calls = sum(
            1 for r in self._sliding_window
            if r.duration > self.config.slow_call_duration_threshold_seconds
        )
        slow_rate = slow_calls / total

        should_open = (
            failure_rate >= self.config.failure_rate_threshold
            or slow_rate >= self.config.slow_call_rate_threshold
        )

        if should_open:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            # 首次 Open:backoff 保持初始值(1)
            # 后续 Open(从 Half-Open 转)在 _check_half_open_completion 中翻倍
            logger.warning(
                "Circuit '%s' CLOSED→OPEN (failure_rate=%.2f, slow_rate=%.2f, backoff x%d)",
                self.name,
                failure_rate,
                slow_rate,
                self._backoff_multiplier,
            )
            # 清空窗口,Half-Open 试探时重新统计
            self._sliding_window.clear()

    def _check_half_open_completion(self) -> None:
        """Half-Open 状态下检查试探是否完成。"""
        # 试探未达上限,继续等待
        if (
            self._half_open_success + self._half_open_failure
            < self.config.permitted_number_of_calls_in_half_open
        ):
            return

        # 试探完成,根据成功率判断
        total = self._half_open_success + self._half_open_failure
        success_rate = self._half_open_success / total

        if success_rate >= (1 - self.config.failure_rate_threshold):
            # 试探成功 → Closed
            self._state = CircuitState.CLOSED
            # 重置 backoff(已恢复)
            self._backoff_multiplier = 1
            self._sliding_window.clear()
            logger.info(
                "Circuit '%s' HALF_OPEN→CLOSED (success=%d/%d)",
                self.name,
                self._half_open_success,
                total,
            )
        else:
            # 试探失败 → 重新 Open(继续翻倍 backoff)
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            self._backoff_multiplier = min(
                self._backoff_multiplier * 2,
                self.config.max_backoff_multiplier,
            )
            logger.warning(
                "Circuit '%s' HALF_OPEN→OPEN (success=%d/%d, backoff x%d)",
                self.name,
                self._half_open_success,
                total,
                self._backoff_multiplier,
            )

        # 重置试探计数
        self._half_open_trials = 0
        self._half_open_success = 0
        self._half_open_failure = 0


# =====================================================================
# 全局熔断器注册中心(按 name 复用同一实例)
# =====================================================================
class CircuitBreakerRegistry:
    """熔断器注册中心 - 按 name 复用实例(避免每处 new 一个)。

    用法:
        from deadman.infrastructure.circuit_breaker import cb_registry
        cb = cb_registry.get_or_create("llm_openai", config=CircuitConfig(...))
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_or_create(
        self,
        name: str,
        config: Optional[CircuitConfig] = None,
    ) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name=name, config=config)
            return self._breakers[name]

    def get(self, name: str) -> Optional[CircuitBreaker]:
        with self._lock:
            return self._breakers.get(name)

    def list_all(self) -> list[CircuitBreaker]:
        with self._lock:
            return list(self._breakers.values())

    def reset_all(self) -> None:
        """重置所有熔断器(测试/运维用)。"""
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()


# 全局单例
cb_registry = CircuitBreakerRegistry()
