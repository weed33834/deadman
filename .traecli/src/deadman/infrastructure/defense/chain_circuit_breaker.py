"""D8:降级链独立熔断(每级降级独立熔断器,防链式失败)。

问题(v1.4 联动风险 10):
    配额超限触发降级 DOWNGRADE_MODEL,降级模型调用失败触发熔断器,
    熔断器 Open 后所有请求 REJECT → 链式降级失败,用户完全无法使用。

    原因:降级链各级共享同一熔断器,任一级失败都拖垮整条链。

缓解:
    1. 降级链每级独立熔断器(gpt-4o / gpt-4o-mini / qwen / fallback rule)
    2. 某级熔断器 Open → 自动跳到下一级(不阻塞)
    3. 末级熔断器(fallback rule)永不 Open(规则模式兜底)
    4. 全链路熔断监控(若全链熔断 → 紧急告警 + 启动只读模式)

设计:
    - DegradationChain: 降级链定义(级联顺序)
    - ChainCircuitBreaker: 链式熔断器(每级独立熔断 + 链式 fallback)
    - ChainCallResult: 调用结果(含 used_level / fallback_reason)

集成:
    llm_client.chat() 内部:
        chain = chain_cb_registry.get_or_create("llm_chat", chain=["gpt-4o","gpt-4o-mini","qwen","rule"])
        result = chain.call(lambda model: invoke(model), context=...)
        # result.used_level 表示实际用了哪一级

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from ..circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitConfig,
    cb_registry,
)
from ..feature_flags import is_enabled

logger = logging.getLogger(__name__)


class FallbackReason(str, Enum):
    """降级原因。"""

    OK = "ok"  # 正常成功
    CB_OPEN = "cb_open"  # 上一级熔断器 Open
    CALL_FAILED = "call_failed"  # 上一级调用失败(异常)
    CALL_TIMEOUT = "call_timeout"  # 上一级超时
    SKIPPED = "skipped"  # 主动跳过(如 budget 不足)
    RULE_FALLBACK = "rule_fallback"  # 末级规则兜底


@dataclass
class ChainCallResult:
    """链式调用结果。"""

    success: bool
    used_level: str  # 实际使用的级(gpt-4o / gpt-4o-mini / qwen / rule)
    used_level_index: int  # 级索引(0 表示顶级)
    fallback_reason: FallbackReason = FallbackReason.OK
    error: str = ""
    duration_seconds: float = 0.0
    skipped_levels: list[str] = field(default_factory=list)  # 跳过的级


class DegradationChain:
    """降级链定义。

    默认 LLM 降级链:
        ["gpt-4o", "gpt-4o-mini", "qwen-max", "deepseek-chat", "rule"]
    rule 是末级兜底(规则模式,永不熔断)。
    """

    def __init__(
        self,
        name: str,
        levels: list[str],
        rule_level: str = "rule",
        configs: Optional[dict[str, CircuitConfig]] = None,
    ) -> None:
        if not levels:
            raise ValueError("DegradationChain requires at least one level")
        self.name = name
        self.levels = levels
        self.rule_level = rule_level
        # 默认末级为 rule_level(若未在 levels 中,自动追加)
        if self.rule_level not in self.levels:
            self.levels.append(self.rule_level)
        self.configs = configs or {}

    def level_config(self, level: str) -> CircuitConfig:
        """获取某级的熔断配置。"""
        if level in self.configs:
            return self.configs[level]
        # rule 级永不熔断(配置宽松)
        if level == self.rule_level:
            return CircuitConfig(
                failure_rate_threshold=1.0,  # 永不触发
                minimum_number_of_calls=10**9,
                wait_duration_in_open_state_seconds=0,
            )
        # 其他级别用默认
        return CircuitConfig()


class ChainCircuitBreaker:
    """链式熔断器 - 每级独立熔断 + 链式 fallback。

    用法:
        chain = ChainCircuitBreaker("llm_chat", ["gpt-4o","gpt-4o-mini","rule"])

        def invoke(model: str):
            if model == "rule":
                return rule_based_response()
            return llm_client.chat(model=model, ...)

        result = chain.call(invoke)
        if result.success:
            print(f"Used {result.used_level}")
    """

    def __init__(self, chain: DegradationChain) -> None:
        self.chain = chain
        self._lock = threading.RLock()
        # 每级独立 CircuitBreaker
        self._breakers: dict[str, CircuitBreaker] = {}
        for level in chain.levels:
            cb_name = f"{chain.name}:{level}"
            self._breakers[level] = cb_registry.get_or_create(
                cb_name, chain.level_config(level)
            )
        # 统计
        self._stats = {
            "total_calls": 0,
            "successful_calls": 0,
            "fallback_count": 0,
            "rule_fallback_count": 0,
            "full_chain_failure": 0,  # 全链失败(罕见,需告警)
        }

    def call(
        self,
        func: Callable[[str], Any],
        *,
        preferred_level: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> ChainCallResult:
        """按降级链调用。

        Args:
            func: callable(level_name) -> result,异常表示失败
            preferred_level: 期望从某级开始(默认从顶级)
            timeout_seconds: 单级调用超时

        Returns:
            ChainCallResult: 调用结果(含实际使用的级)
        """
        if not is_enabled("defense"):
            # 关闭:直接调顶级
            try:
                start = time.time()
                result = func(self.chain.levels[0])
                return ChainCallResult(
                    success=True,
                    used_level=self.chain.levels[0],
                    used_level_index=0,
                    duration_seconds=time.time() - start,
                )
            except Exception as e:
                return ChainCallResult(
                    success=False,
                    used_level=self.chain.levels[0],
                    used_level_index=0,
                    fallback_reason=FallbackReason.CALL_FAILED,
                    error=str(e),
                )

        start_levels = self.chain.levels
        if preferred_level and preferred_level in self.chain.levels:
            idx = self.chain.levels.index(preferred_level)
            start_levels = self.chain.levels[idx:]

        with self._lock:
            self._stats["total_calls"] += 1

        skipped: list[str] = []
        for idx, level in enumerate(start_levels):
            cb = self._breakers[level]
            # 1. 检查熔断器状态
            try:
                cb.acquire()
            except CircuitBreakerOpenError:
                skipped.append(level)
                logger.info(
                    "Chain %s level %s CB open, skip",
                    self.chain.name, level,
                )
                continue

            # 2. 调用
            start = time.time()
            try:
                # 简单超时(同步);生产可换 asyncio.wait_for
                result = self._call_with_timeout(func, level, timeout_seconds)
                duration = time.time() - start
                cb.release_success(duration)
                with self._lock:
                    self._stats["successful_calls"] += 1
                    if idx > 0:
                        self._stats["fallback_count"] += 1
                    if level == self.chain.rule_level:
                        self._stats["rule_fallback_count"] += 1
                actual_idx = self.chain.levels.index(level)
                return ChainCallResult(
                    success=True,
                    used_level=level,
                    used_level_index=actual_idx,
                    duration_seconds=duration,
                    skipped_levels=skipped,
                )
            except Exception as e:
                duration = time.time() - start
                cb.release_failure(duration, error=e)
                skipped.append(level)
                logger.warning(
                    "Chain %s level %s failed: %s, fallback to next",
                    self.chain.name, level, e,
                )
                continue

        # 全链失败(rule 级也失败,严重)
        with self._lock:
            self._stats["full_chain_failure"] += 1
        logger.critical(
            "Chain %s FULL FAILURE - all levels failed (skipped=%s)",
            self.chain.name, skipped,
        )
        return ChainCallResult(
            success=False,
            used_level=self.chain.rule_level,
            used_level_index=len(self.chain.levels) - 1,
            fallback_reason=FallbackReason.RULE_FALLBACK,
            error="all levels failed",
            skipped_levels=skipped,
        )

    def _call_with_timeout(
        self,
        func: Callable[[str], Any],
        level: str,
        timeout: float,
    ) -> Any:
        """简单超时实现(线程版本)。

        生产环境推荐用 asyncio.wait_for。
        """
        # rule 级不超时(规则模式很快)
        if level == self.chain.rule_level:
            return func(level)

        result_holder: dict[str, Any] = {}

        def _runner() -> None:
            try:
                result_holder["value"] = func(level)
            except Exception as e:
                result_holder["error"] = e

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            raise TimeoutError(f"Level {level} timeout after {timeout}s")
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("value")

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        with self._lock:
            stats = dict(self._stats)
        # 各级熔断器状态(通过 get_metrics 获取,避免直接访问内部字段)
        stats["levels"] = {}
        for level, cb in self._breakers.items():
            try:
                m = cb.get_metrics()
                stats["levels"][level] = {
                    "state": m.get("state", "unknown"),
                    "failure_rate": m.get("failure_rate", 0.0),
                    "total_calls": m.get("total_calls", 0),
                }
            except Exception as e:
                stats["levels"][level] = {"error": str(e)}
        return stats

    def reset(self) -> None:
        """重置所有熔断器(管理员手动)。"""
        for cb in self._breakers.values():
            try:
                cb.reset()
            except Exception as e:
                logger.debug("熔断器 reset 失败: %s", e)
        with self._lock:
            self._stats = {
                "total_calls": 0,
                "successful_calls": 0,
                "fallback_count": 0,
                "rule_fallback_count": 0,
                "full_chain_failure": 0,
            }


# =====================================================================
# Registry
# =====================================================================

_chain_registry: dict[str, ChainCircuitBreaker] = {}
_chain_lock = threading.Lock()


def get_or_create_chain(
    name: str,
    levels: list[str],
    rule_level: str = "rule",
    configs: Optional[dict[str, CircuitConfig]] = None,
) -> ChainCircuitBreaker:
    """获取或创建降级链。"""
    with _chain_lock:
        if name not in _chain_registry:
            chain = DegradationChain(
                name=name,
                levels=list(levels),
                rule_level=rule_level,
                configs=configs,
            )
            _chain_registry[name] = ChainCircuitBreaker(chain)
        return _chain_registry[name]


def list_chains() -> dict[str, dict[str, Any]]:
    """列出所有链 + 统计。"""
    with _chain_lock:
        return {name: cb.get_stats() for name, cb in _chain_registry.items()}


def reset_all_chains() -> None:
    """重置所有链(测试 / 管理员)。"""
    with _chain_lock:
        for cb in _chain_registry.values():
            cb.reset()
