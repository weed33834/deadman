"""D7:级联故障防护(依赖链路故障检测 + 隔离)。

问题:
    deadman 调用链:
        React Loop → Debate → 3 个 LLM Agent → vector_store → DB

    任一中间节点故障:
        - DB 挂 → vector_store 超时 → debate 失败 → react_loop 卡死
        - LLM provider 挂 → 多个 agent 并发失败 → 熔断器未及时 Open

    缓解:
        - 显式依赖图:记录每个组件的上下游依赖
        - 故障传播检测:某节点故障时,标记所有下游节点为"依赖故障"
        - 自动隔离:故障节点跳过(返回 cached / fallback),不阻塞上游
        - 故障恢复:节点恢复后自动清除标记

设计:
    - DependencyNode: 节点(LLM / vector_store / DB / tool / ...)
    - DependencyState: 节点状态(HEALTHY / DEGRADED / FAILED / ISOLATED)
    - CascadingGuard: 依赖图管理 + 故障传播

集成:
    react_loop.py 调用 LLM 前检查:
        guard = get_cascading_guard()
        if guard.is_healthy("llm_openai"):
            result = await llm.chat(...)
        else:
            # 跳过(降级到 cached / fallback)
            result = cached_or_fallback

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


class DependencyState(str, Enum):
    """节点状态:

    HEALTHY → DEGRADED → FAILED → ISOLATED(自动隔离,跳过调用)
                                            ↓
                                        HEALTHY(恢复)
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # 性能下降(慢但可用)
    FAILED = "failed"  # 失败(无法调用)
    ISOLATED = "isolated"  # 已隔离(主动跳过)


@dataclass
class DependencyNode:
    """依赖节点。"""

    name: str  # llm_openai / vector_store / db / ...
    state: DependencyState = DependencyState.HEALTHY
    # 显式依赖(谁依赖我)
    depended_by: set[str] = field(default_factory=set)
    # 我依赖谁
    depends_on: set[str] = field(default_factory=set)
    # 状态时间戳
    state_since: float = field(default_factory=time.time)
    # 故障计数(连续失败次数)
    failure_count: int = 0
    # 最近一次成功
    last_success_at: float | None = None
    # 最近一次失败
    last_failure_at: float | None = None
    # 故障原因
    last_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        d["depended_by"] = list(self.depended_by)
        d["depends_on"] = list(self.depends_on)
        return d


@dataclass
class FailureEvent:
    """故障事件(用于审计)。"""

    timestamp: float
    node: str
    error: str
    propagated_to: list[str] = field(default_factory=list)
    isolated: bool = False


class CascadingGuard:
    """级联故障防护器。

    用法:
        guard = get_cascading_guard()
        # 注册依赖关系
        guard.register("react_loop", depends_on=["llm_openai", "tool_search"])
        guard.register("debate", depends_on=["llm_openai", "llm_anthropic"])
        guard.register("llm_openai", depended_by=["react_loop", "debate"])

        # 调用前检查
        if guard.is_healthy("llm_openai"):
            result = await llm.chat(...)
            guard.record_success("llm_openai")
        else:
            guard.record_failure("llm_openai", error="timeout")
            # 降级
            result = fallback

        # 故障传播:llm_openai 失败 → react_loop / debate 自动标记 DEGRADED
    """

    def __init__(
        self,
        # 连续失败 N 次后转 FAILED
        failure_threshold: int = 3,
        # FAILED 后多久转 ISOLATED(主动隔离)
        isolation_after_seconds: float = 30.0,
        # ISOLATED 后多久尝试恢复(转 HEALTHY)
        recovery_probe_after_seconds: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.isolation_after_seconds = isolation_after_seconds
        self.recovery_probe_after_seconds = recovery_probe_after_seconds
        self._lock = threading.RLock()
        self._nodes: dict[str, DependencyNode] = {}
        # 故障事件历史
        self._failure_events: deque[FailureEvent] = deque(maxlen=10_000)

    def register(
        self,
        name: str,
        depends_on: list[str] | None = None,
        depended_by: list[str] | None = None,
    ) -> None:
        """注册节点 + 依赖关系。"""
        with self._lock:
            if name not in self._nodes:
                self._nodes[name] = DependencyNode(name=name)
            node = self._nodes[name]
            for dep in depends_on or []:
                node.depends_on.add(dep)
                # 反向注册:dep 被 name 依赖
                if dep not in self._nodes:
                    self._nodes[dep] = DependencyNode(name=dep)
                self._nodes[dep].depended_by.add(name)
            for parent in depended_by or []:
                node.depended_by.add(parent)
                if parent not in self._nodes:
                    self._nodes[parent] = DependencyNode(name=parent)
                self._nodes[parent].depends_on.add(name)

    def is_healthy(self, name: str) -> bool:
        """节点是否健康(可调用)。"""
        if not is_enabled("defense"):
            return True

        with self._lock:
            self._maybe_recover(name)
            node = self._nodes.get(name)
            if node is None:
                return True  # 未注册 = 不限制
            # HEALTHY / DEGRADED 可用,FAILED / ISOLATED 不可用
            return node.state in (DependencyState.HEALTHY, DependencyState.DEGRADED)

    def record_success(self, name: str) -> None:
        """记录节点成功调用。"""
        if not is_enabled("defense"):
            return
        with self._lock:
            node = self._nodes.get(name)
            if node is None:
                return
            node.failure_count = 0
            node.last_success_at = time.time()
            # DEGRADED / FAILED → 恢复 HEALTHY
            if node.state in (DependencyState.DEGRADED, DependencyState.FAILED):
                self._set_state(node, DependencyState.HEALTHY)
                logger.info("Node %s recovered to HEALTHY", name)

    def record_failure(self, name: str, error: str = "") -> None:
        """记录节点失败调用。"""
        if not is_enabled("defense"):
            return
        with self._lock:
            node = self._nodes.get(name)
            if node is None:
                # 自动注册
                node = DependencyNode(name=name)
                self._nodes[name] = node

            node.failure_count += 1
            node.last_failure_at = time.time()
            node.last_error = error

            # 故障事件记录
            event = FailureEvent(
                timestamp=time.time(),
                node=name,
                error=error,
            )
            self._failure_events.append(event)

            # 状态转换
            if node.failure_count >= self.failure_threshold:
                if node.state != DependencyState.FAILED:
                    # 标记 FAILED,传播到下游
                    event.propagated_to = self._propagate_failure(name)
                    self._set_state(node, DependencyState.FAILED)
                    logger.warning(
                        "Node %s FAILED (failures=%d, error=%s, propagated_to=%s)",
                        name,
                        node.failure_count,
                        error,
                        event.propagated_to,
                    )

    def isolate(self, name: str, reason: str = "") -> None:
        """手动隔离节点(运维用)。"""
        with self._lock:
            node = self._nodes.get(name)
            if node is None:
                return
            self._set_state(node, DependencyState.ISOLATED)
            # 传播隔离到下游
            self._propagate_failure(name)
            logger.warning("Node %s ISOLATED (reason=%s)", name, reason)

    def recover(self, name: str) -> None:
        """手动恢复节点(运维用)。"""
        with self._lock:
            node = self._nodes.get(name)
            if node is None:
                return
            self._set_state(node, DependencyState.HEALTHY)
            node.failure_count = 0
            # 下游也可能恢复(若没其他故障)
            for dep in list(node.depended_by):
                self._maybe_recover(dep)
            logger.info("Node %s manually recovered", name)

    def get_state(self, name: str) -> DependencyState:
        with self._lock:
            self._maybe_recover(name)
            node = self._nodes.get(name)
            return node.state if node else DependencyState.HEALTHY

    def list_nodes(self) -> list[dict[str, Any]]:
        """列出所有节点(看板用)。"""
        with self._lock:
            for name in list(self._nodes.keys()):
                self._maybe_recover(name)
            return [n.to_dict() for n in self._nodes.values()]

    def get_failure_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._failure_events)[-limit:]
            return [
                {
                    "timestamp": e.timestamp,
                    "node": e.node,
                    "error": e.error,
                    "propagated_to": e.propagated_to,
                    "isolated": e.isolated,
                }
                for e in events
            ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_state = {state.value: 0 for state in DependencyState}
            for node in self._nodes.values():
                by_state[node.state.value] += 1
            return {
                "total_nodes": len(self._nodes),
                "by_state": by_state,
                "failure_events_total": len(self._failure_events),
            }

    # ==================================================================
    # 内部
    # ==================================================================

    def _set_state(self, node: DependencyNode, new_state: DependencyState) -> None:
        if node.state == new_state:
            return
        node.state = new_state
        node.state_since = time.time()

    def _propagate_failure(self, failed_node: str) -> list[str]:
        """故障传播:把下游节点标记为 DEGRADED。"""
        propagated: list[str] = []
        node = self._nodes.get(failed_node)
        if node is None:
            return propagated
        for downstream_name in node.depended_by:
            downstream = self._nodes.get(downstream_name)
            if downstream is None:
                continue
            if downstream.state == DependencyState.HEALTHY:
                self._set_state(downstream, DependencyState.DEGRADED)
                propagated.append(downstream_name)
                logger.info(
                    "Node %s DEGRADED due to upstream %s failure",
                    downstream_name,
                    failed_node,
                )
        return propagated

    def _maybe_recover(self, name: str) -> None:
        """检查是否可恢复(ISOLATED 后超时 → HEALTHY 试探)。"""
        node = self._nodes.get(name)
        if node is None:
            return
        now = time.time()
        # ISOLATED 后超时 → 试探性 HEALTHY
        if node.state == DependencyState.ISOLATED:
            if now - node.state_since > self.recovery_probe_after_seconds:
                self._set_state(node, DependencyState.HEALTHY)
                node.failure_count = 0
                logger.info("Node %s probed recovery (ISOLATED→HEALTHY)", name)
        # FAILED 后超时 → ISOLATED(主动隔离)
        elif node.state == DependencyState.FAILED:
            if now - node.state_since > self.isolation_after_seconds:
                self._set_state(node, DependencyState.ISOLATED)
                logger.warning("Node %s auto-isolated (FAILED→ISOLATED)", name)


# 全局单例
_cg_instance: CascadingGuard | None = None
_cg_lock = threading.Lock()


def get_cascading_guard() -> CascadingGuard:
    global _cg_instance
    if _cg_instance is None:
        with _cg_lock:
            if _cg_instance is None:
                _cg_instance = CascadingGuard()
    return _cg_instance
