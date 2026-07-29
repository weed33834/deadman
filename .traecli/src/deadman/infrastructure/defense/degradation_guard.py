"""D6:降级风暴防护(防止多机制同时降级导致服务完全不可用)。

问题:
    多个独立机制各自有降级策略:
        - quota.py:超限降级模型(gpt-4o → gpt-4o-mini)
        - circuit_breaker.py:Open 时快速失败
        - cost_router.py:无候选时返回 None
        - react_loop.py:stuck 时降级到简单回复

    场景:租户超 quota → 4 个机制同时触发降级
        1. quota 强制 DOWNGRADE_MODEL(切 mini 模型)
        2. mini 模型 + circuit_breaker 触发(因为 mini 也限流)
        3. circuit_breaker Open → cost_router 返回 None
        4. cost_router None → react_loop stuck → 降级简单回复

    最终:用户收到无意义回复(全部降级叠加),而非合理的"配额不足"提示。

缓解:
    - DegradationGuard:全局降级计数器
    - 降级阈值:同时降级超过 N 个机制 → 拒绝执行(返回明确错误而非叠加降级)
    - 降级优先级:高优先级降级时阻止低优先级降级(避免雪崩)

设计:
    - DegradationLevel: 降级级别(NONE / SOFT / HARD / CRITICAL)
    - DegradationEvent: 单次降级事件
    - DegradationGuard: 全局降级守卫(防叠加)

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..feature_flags import is_enabled

logger = logging.getLogger(__name__)


class DegradationLevel(str, Enum):
    """降级级别(从轻到重)。"""

    NONE = "none"        # 无降级
    SOFT = "soft"        # 软降级(模型降级 / 跳过非核心功能)
    HARD = "hard"        # 硬降级(限速 / 禁用功能 / 简化回复)
    CRITICAL = "critical"  # 关键降级(拒绝服务)


# 各机制的默认降级级别
MECHANISM_DEFAULT_LEVEL: dict[str, DegradationLevel] = {
    "quota.downgrade_model": DegradationLevel.SOFT,
    "quota.rate_limit": DegradationLevel.SOFT,
    "quota.disable_feature": DegradationLevel.HARD,
    "quota.reject": DegradationLevel.CRITICAL,
    "circuit_breaker.open": DegradationLevel.HARD,
    "cost_router.no_candidate": DegradationLevel.HARD,
    "react_loop.stuck": DegradationLevel.SOFT,
    "react_loop.budget_exceeded": DegradationLevel.HARD,
    "debate.budget_exceeded": DegradationLevel.SOFT,
    "memory.compress_failed": DegradationLevel.SOFT,
    "vector_store.fallback_inmemory": DegradationLevel.SOFT,
    "multi_tenant.quota_exceeded": DegradationLevel.HARD,
    "billing.payment_overdue": DegradationLevel.HARD,
}


@dataclass
class DegradationEvent:
    """单次降级事件。"""

    timestamp: float
    mechanism: str  # quota.downgrade_model / circuit_breaker.open / ...
    level: DegradationLevel
    scope: str  # global / tenant:t1 / user:u1
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # 自动恢复时间(若超过此时间未恢复,触发警报)
    expected_recovery_at: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.value
        return d


class DegradationGuard:
    """降级守卫(防降级风暴)。

    用法:
        guard = get_degradation_guard()

        # 在降级前检查
        if guard.can_degrade("quota.downgrade_model", scope="tenant:t1"):
            guard.record(DegradationEvent(
                timestamp=time.time(),
                mechanism="quota.downgrade_model",
                level=DegradationLevel.SOFT,
                scope="tenant:t1",
                reason="user quota at 90%",
            ))
            # 执行降级
            downgrade_to = "gpt-4o-mini"
        else:
            # 已有太多降级,直接拒绝(避免叠加)
            raise RuntimeError("Quota exceeded, please upgrade plan")

    防护逻辑:
        1. 同一 scope 内,同时活跃的 HARD 降级 ≥ 2 个 → 拒绝新降级(返回明确错误)
        2. 同一 scope 内,任何 CRITICAL 降级 → 拒绝所有新降级(直接拒绝)
        3. 跨 scope 全局降级计数 ≥ 阈值 → 触发警报
    """

    def __init__(
        self,
        # 同一 scope 内最大同时降级数(HARD 级)
        max_hard_per_scope: int = 2,
        # 同一 scope 内最大同时降级数(SOFT 级)
        max_soft_per_scope: int = 5,
        # 全局最大同时降级数(所有 scope 合计)
        max_global_active: int = 50,
        # 降级事件保留时长(秒)
        event_retention_seconds: int = 3600,
    ) -> None:
        self.max_hard_per_scope = max_hard_per_scope
        self.max_soft_per_scope = max_soft_per_scope
        self.max_global_active = max_global_active
        self.event_retention_seconds = event_retention_seconds
        self._lock = threading.RLock()
        # 活跃降级事件:{scope: [DegradationEvent]}
        self._active: dict[str, list[DegradationEvent]] = {}
        # 历史降级事件(用于审计 / 看板)
        self._history: deque[DegradationEvent] = deque(maxlen=10_000)

    def can_degrade(
        self,
        mechanism: str,
        scope: str,
        level: DegradationLevel | None = None,
    ) -> bool:
        """检查是否可以降级(降级前调用)。"""
        if not is_enabled("defense"):
            return True  # 关闭:允许(向后兼容)

        actual_level = level or MECHANISM_DEFAULT_LEVEL.get(mechanism, DegradationLevel.SOFT)

        with self._lock:
            self._cleanup_expired()
            scope_events = self._active.get(scope, [])
            hard_count = sum(1 for e in scope_events if e.level == DegradationLevel.HARD)
            soft_count = sum(1 for e in scope_events if e.level == DegradationLevel.SOFT)
            critical_count = sum(1 for e in scope_events if e.level == DegradationLevel.CRITICAL)
            global_count = sum(len(events) for events in self._active.values())

            # CRITICAL → 拒绝所有新降级
            if critical_count > 0:
                logger.warning(
                    "Cannot degrade %s (scope=%s): CRITICAL active",
                    mechanism, scope,
                )
                return False

            # 全局上限
            if global_count >= self.max_global_active:
                logger.warning(
                    "Cannot degrade %s: global active=%d (max=%d)",
                    mechanism, global_count, self.max_global_active,
                )
                return False

            # HARD 上限
            if actual_level == DegradationLevel.HARD and hard_count >= self.max_hard_per_scope:
                logger.warning(
                    "Cannot degrade %s to HARD (scope=%s): hard active=%d (max=%d)",
                    mechanism, scope, hard_count, self.max_hard_per_scope,
                )
                return False

            # SOFT 上限
            if actual_level == DegradationLevel.SOFT and soft_count >= self.max_soft_per_scope:
                logger.warning(
                    "Cannot degrade %s to SOFT (scope=%s): soft active=%d (max=%d)",
                    mechanism, scope, soft_count, self.max_soft_per_scope,
                )
                return False

            return True

    def record(self, event: DegradationEvent) -> bool:
        """记录降级事件(若 can_degrade 返回 True 则记录)。

        Returns:
            是否成功记录(can_degrade 拒绝则返回 False)
        """
        if not is_enabled("defense"):
            return True

        if not self.can_degrade(event.mechanism, event.scope, event.level):
            return False

        with self._lock:
            self._active.setdefault(event.scope, []).append(event)
            self._history.append(event)
        return True

    def recover(
        self,
        mechanism: str,
        scope: str,
    ) -> bool:
        """标记降级已恢复(从活跃列表移除)。"""
        if not is_enabled("defense"):
            return True

        with self._lock:
            events = self._active.get(scope, [])
            before = len(events)
            self._active[scope] = [
                e for e in events if e.mechanism != mechanism
            ]
            after = len(self._active[scope])
            if not self._active[scope]:
                del self._active[scope]
            return before > after

    def get_active(self, scope: str | None = None) -> list[DegradationEvent]:
        """获取当前活跃降级事件。"""
        with self._lock:
            self._cleanup_expired()
            if scope:
                return list(self._active.get(scope, []))
            result: list[DegradationEvent] = []
            for events in self._active.values():
                result.extend(events)
            return result

    def get_history(
        self,
        limit: int = 100,
        scope: str | None = None,
    ) -> list[DegradationEvent]:
        """获取历史降级事件(审计用)。"""
        with self._lock:
            events = list(self._history)
            if scope:
                events = [e for e in events if e.scope == scope]
            return events[-limit:]

    def get_level(self, scope: str) -> DegradationLevel:
        """获取某 scope 的当前降级级别(最高级)。"""
        with self._lock:
            events = self._active.get(scope, [])
            if not events:
                return DegradationLevel.NONE
            levels = [e.level for e in events]
            # 优先级:CRITICAL > HARD > SOFT > NONE
            if DegradationLevel.CRITICAL in levels:
                return DegradationLevel.CRITICAL
            if DegradationLevel.HARD in levels:
                return DegradationLevel.HARD
            if DegradationLevel.SOFT in levels:
                return DegradationLevel.SOFT
            return DegradationLevel.NONE

    def stats(self) -> dict[str, Any]:
        """获取统计(看板用)。"""
        with self._lock:
            self._cleanup_expired()
            total = sum(len(events) for events in self._active.values())
            by_level = {level.value: 0 for level in DegradationLevel}
            for events in self._active.values():
                for e in events:
                    by_level[e.level.value] += 1
            return {
                "total_active": total,
                "scopes_affected": len(self._active),
                "by_level": by_level,
                "history_total": len(self._history),
            }

    # ==================================================================
    # 内部
    # ==================================================================

    def _cleanup_expired(self) -> None:
        """清理超时未恢复的降级(防止永久降级)。"""
        now = time.time()
        cutoff = now - self.event_retention_seconds
        for scope in list(self._active.keys()):
            self._active[scope] = [
                e for e in self._active[scope]
                if e.expected_recovery_at is None or e.expected_recovery_at > cutoff
            ]
            if not self._active[scope]:
                del self._active[scope]


# 全局单例
_dg_instance: DegradationGuard | None = None
_dg_lock = threading.Lock()


def get_degradation_guard() -> DegradationGuard:
    global _dg_instance
    if _dg_instance is None:
        with _dg_lock:
            if _dg_instance is None:
                _dg_instance = DegradationGuard()
    return _dg_instance
