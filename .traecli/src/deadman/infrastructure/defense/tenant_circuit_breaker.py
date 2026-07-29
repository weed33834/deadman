"""D2:熔断器按租户隔离(防止单租户拖垮全平台)。

问题:
    cost_router.py 使用 `cb_registry.get_or_create("llm_openai")` → 全局共享熔断器。
    若 tenant_A 高频调用 LLM 触发熔断 → tenant_B / C 也无法调用 LLM。

    缓解:熔断器 name 加 tenant_id 前缀,每租户独立熔断状态。

设计:
    - TenantCircuitBreaker: 包装 CircuitBreaker,内部按 tenant 隔离
    - 自动注入 tenant_id(从 ContextVar 获取)
    - 命名约定:`<circuit_name>:<tenant_id>`

集成:
    cost_router.py 升级:
        cb = get_tenant_cb(f"llm_{provider}")  # 自动加 tenant
    代替:
        cb = cb_registry.get_or_create(f"llm_{provider}")

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from ..feature_flags import is_enabled
from ..multi_tenant import get_current_tenant_id
from ..circuit_breaker import (
    CircuitBreaker,
    CircuitConfig,
    CircuitState,
    cb_registry,
)

logger = logging.getLogger(__name__)


class TenantCircuitBreaker:
    """按租户隔离的熔断器包装。

    每次调用 acquire/release 时,自动根据当前 tenant_id 路由到对应的 CircuitBreaker 实例。

    用法:
        tcb = TenantCircuitBreaker("llm_openai")
        try:
            tcb.acquire()  # 自动用当前租户的 circuit
            result = await llm.chat(...)
            tcb.release_success(duration=...)
        except CircuitBreakerOpenError:
            # 当前租户熔断,其他租户不受影响
            ...
    """

    def __init__(
        self,
        base_name: str,
        config: Optional[CircuitConfig] = None,
    ) -> None:
        self.base_name = base_name
        self.config = config
        self._lock = threading.RLock()
        # 缓存 tenant_id → CircuitBreaker
        self._tenant_breakers: dict[str, CircuitBreaker] = {}

    def acquire(self, tenant_id: Optional[str] = None) -> str:
        """按当前租户 acquire。"""
        if not is_enabled("defense"):
            # 关闭:直接走原熔断器(全局,向后兼容)
            return self._get_global_breaker().acquire()

        tid = tenant_id or get_current_tenant_id()
        cb = self._get_or_create(tid)
        return cb.acquire()

    def release_success(
        self,
        duration: float = 0.0,
        tenant_id: Optional[str] = None,
    ) -> None:
        if not is_enabled("defense"):
            self._get_global_breaker().release_success(duration)
            return
        tid = tenant_id or get_current_tenant_id()
        cb = self._get_or_create(tid)
        cb.release_success(duration)

    def release_failure(
        self,
        duration: float = 0.0,
        error: Optional[Exception] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        if not is_enabled("defense"):
            self._get_global_breaker().release_failure(duration, error)
            return
        tid = tenant_id or get_current_tenant_id()
        cb = self._get_or_create(tid)
        cb.release_failure(duration, error)

    def get_state(self, tenant_id: Optional[str] = None) -> CircuitState:
        """获取指定租户的熔断器状态。"""
        if not is_enabled("defense"):
            return self._get_global_breaker().state
        tid = tenant_id or get_current_tenant_id()
        cb = self._get_or_create(tid)
        return cb.state

    def reset_tenant(self, tenant_id: str) -> None:
        """重置指定租户的熔断器(管理用)。"""
        with self._lock:
            cb = self._tenant_breakers.get(tenant_id)
            if cb:
                cb.reset()

    def reset_all(self) -> None:
        """重置所有租户的熔断器(测试用)。"""
        with self._lock:
            for cb in self._tenant_breakers.values():
                cb.reset()
            # 全局也重置
            self._get_global_breaker().reset()

    def list_tenant_states(self) -> dict[str, dict]:
        """列出所有租户的熔断器状态(看板用)。"""
        with self._lock:
            return {
                tid: cb.get_metrics()
                for tid, cb in self._tenant_breakers.items()
            }

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_or_create(self, tenant_id: str) -> CircuitBreaker:
        with self._lock:
            if tenant_id not in self._tenant_breakers:
                # 命名约定:<base>:<tenant_id>
                cb_name = f"{self.base_name}:{tenant_id}"
                self._tenant_breakers[tenant_id] = cb_registry.get_or_create(
                    cb_name, self.config,
                )
            return self._tenant_breakers[tenant_id]

    def _get_global_breaker(self) -> CircuitBreaker:
        """关闭 defense 时退回全局熔断器。"""
        return cb_registry.get_or_create(self.base_name, self.config)


class TenantCircuitBreakerRegistry:
    """TenantCircuitBreaker 全局注册中心(按 base_name 复用)。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenant_cbs: dict[str, TenantCircuitBreaker] = {}

    def get_or_create(
        self,
        base_name: str,
        config: Optional[CircuitConfig] = None,
    ) -> TenantCircuitBreaker:
        with self._lock:
            if base_name not in self._tenant_cbs:
                self._tenant_cbs[base_name] = TenantCircuitBreaker(base_name, config)
            return self._tenant_cbs[base_name]

    def list_all(self) -> list[TenantCircuitBreaker]:
        with self._lock:
            return list(self._tenant_cbs.values())


# 全局单例
_tcb_registry = TenantCircuitBreakerRegistry()


def get_tenant_cb(
    base_name: str,
    config: Optional[CircuitConfig] = None,
) -> TenantCircuitBreaker:
    """获取按租户隔离的熔断器(主入口)。"""
    return _tcb_registry.get_or_create(base_name, config)
