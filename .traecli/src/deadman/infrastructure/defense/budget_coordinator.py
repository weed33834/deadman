"""D1 + D3:跨会话用户级 budget 隔离 + 全局 token budget 协调。

问题(D1):
    quota.py 按租户配额,但同一用户开多个会话,各自消耗独立 budget → 用户可绕过单会话上限。

    场景:
        user_A 在 session_1 消耗 80% 配额(已 WARN)
        user_A 在 session_2 又开新会话 → budget 重置 → 继续消耗
        结果:用户实际消耗 160%,远超配额。

    缓解:所有 session 共享同一 user_id 的累计消耗(跨会话 budget)。

问题(D3):
    多个机制各自有 budget,叠加导致成本失控:
        - React 循环:max_iterations
        - quota.py:llm_tokens 配额
        - cost_router.py:estimated_cost
        - debate.py:总 token 上限 8000
        - reflexion.py:反思深度

    场景:用户调用 debate,debate 内部 3 轮 × 3 个 agent = 9 次 LLM,
        每次又被 React 循环包裹 → 总 token 数 = 9 × N × react_iter,
        远超用户预期。

    缓解:统一 BudgetCoordinator,所有机制消费前先扣减全局 budget,
        超限则拒绝执行(返回 budget_exceeded,降级而非全跑)。

设计:
    - BudgetScope: budget 维度(用户 / 租户 / 全局)
    - BudgetAllocation: 单次分配记录
    - BudgetCoordinator: 统一协调器(避免多机制各自扣减)

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..feature_flags import is_enabled
from ..multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


class BudgetScope(str, Enum):
    """budget 作用域(优先级高→低,父域超限则子域拒绝)。"""

    GLOBAL = "global"  # 全局 budget(平台级成本控制)
    TENANT = "tenant"  # 租户级(配额限制)
    USER = "user"  # 用户级(防绕过)
    SESSION = "session"  # 会话级(防单次爆炸)


class BudgetDimension(str, Enum):
    """budget 维度。"""

    LLM_TOKENS = "llm_tokens"
    TOOL_CALLS = "tool_calls"
    LLM_CALLS = "llm_calls"  # LLM 调用次数(独立于 token)
    MULTI_TURN = "multi_turn"  # 多轮交互次数
    CONCURRENT = "concurrent"  # 并发请求数


@dataclass
class BudgetAllocation:
    """单次 budget 分配记录。"""

    allocation_id: str
    scope: BudgetScope
    scope_id: str  # user_id / tenant_id / "global" / session_id
    dimension: BudgetDimension
    amount: int  # 分配量
    consumer: str  # 消费者(react_loop / debate / cost_router / ...)
    request_id: str = ""  # 关联到 trace
    timestamp: float = field(default_factory=time.time)
    # 释放相关
    released: bool = False
    released_at: Optional[float] = None
    actual_used: int = 0  # 实际使用量(<= amount)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scope"] = self.scope.value
        d["dimension"] = self.dimension.value
        return d


class BudgetExceededError(Exception):
    """budget 超限(应降级而非抛异常,但提供信号)。"""

    def __init__(self, scope: BudgetScope, dimension: BudgetDimension,
                 used: int, limit: int, consumer: str) -> None:
        self.scope = scope
        self.dimension = dimension
        self.used = used
        self.limit = limit
        self.consumer = consumer
        super().__init__(
            f"Budget exceeded: scope={scope.value}, dim={dimension.value}, "
            f"used={used}/{limit} (consumer={consumer})"
        )


class BudgetCoordinator:
    """全局 budget 协调器。

    设计:
        - 多层 budget(GLOBAL > TENANT > USER > SESSION),父域超限则子域拒绝
        - 预扣减(pre-allocation)机制:执行前先 allocate,执行后 release(used)
        - 多 consumer 协调:react_loop / debate / cost_router 都从这里扣
        - 持久化:跨会话累积(避免用户重开 session 重置)

    用法:
        bc = get_budget_coordinator()
        # 预扣减
        alloc = bc.allocate(
            scope=BudgetScope.USER,
            scope_id="user_A",
            dimension=BudgetDimension.LLM_TOKENS,
            amount=1000,
            consumer="react_loop",
        )
        # 执行 LLM 调用...
        # 释放(实际用了 850)
        bc.release(alloc.allocation_id, actual_used=850)
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        # 默认上限(可被租户级 / 用户级覆盖)
        global_limits: Optional[dict[BudgetDimension, int]] = None,
        tenant_limits: Optional[dict[BudgetDimension, int]] = None,
        user_limits: Optional[dict[BudgetDimension, int]] = None,
        session_limits: Optional[dict[BudgetDimension, int]] = None,
    ) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_BUDGET_STORE", "data/defense/budget.json")
        )
        self._lock = threading.RLock()
        # 累计已用:{scope: {scope_id: {dimension: total_used}}}
        self._usage: dict[BudgetScope, dict[str, dict[BudgetDimension, int]]] = {
            BudgetScope.GLOBAL: {},
            BudgetScope.TENANT: {},
            BudgetScope.USER: {},
            BudgetScope.SESSION: {},
        }
        # 待释放的预分配:{allocation_id: BudgetAllocation}
        self._allocations: dict[str, BudgetAllocation] = {}
        # 细粒度覆盖: {(scope, scope_id): {dimension: limit}}
        # 用于 TENANT/USER 级别的 per-entity 限额覆盖
        self._scope_overrides: dict[tuple[str, str], dict[BudgetDimension, int]] = {}
        # 上限配置
        self._limits: dict[BudgetScope, dict[BudgetDimension, int]] = {
            BudgetScope.GLOBAL: global_limits or {
                BudgetDimension.LLM_TOKENS: 10_000_000,  # 平台每日上限
                BudgetDimension.TOOL_CALLS: 100_000,
                BudgetDimension.LLM_CALLS: 50_000,
                BudgetDimension.MULTI_TURN: 200_000,
                BudgetDimension.CONCURRENT: 1000,
            },
            BudgetScope.TENANT: tenant_limits or {
                BudgetDimension.LLM_TOKENS: 1_000_000,
                BudgetDimension.TOOL_CALLS: 10_000,
                BudgetDimension.LLM_CALLS: 5_000,
                BudgetDimension.MULTI_TURN: 20_000,
                BudgetDimension.CONCURRENT: 100,
            },
            BudgetScope.USER: user_limits or {
                BudgetDimension.LLM_TOKENS: 100_000,
                BudgetDimension.TOOL_CALLS: 1_000,
                BudgetDimension.LLM_CALLS: 500,
                BudgetDimension.MULTI_TURN: 2_000,
                BudgetDimension.CONCURRENT: 10,
            },
            BudgetScope.SESSION: session_limits or {
                BudgetDimension.LLM_TOKENS: 10_000,
                BudgetDimension.TOOL_CALLS: 100,
                BudgetDimension.LLM_CALLS: 50,
                BudgetDimension.MULTI_TURN: 100,
                BudgetDimension.CONCURRENT: 5,
            },
        }
        self._loaded = False

    def allocate(
        self,
        scope: BudgetScope,
        scope_id: str,
        dimension: BudgetDimension,
        amount: int,
        consumer: str,
        request_id: str = "",
        # 是否严格(严格则超限抛异常,非严格则降级)
        strict: bool = False,
    ) -> Optional[BudgetAllocation]:
        """预扣减 budget。

        检查所有父域:
            - SESSION → USER → TENANT → GLOBAL,任一超限则拒绝
        """
        if not is_enabled("defense"):
            # 关闭:返回虚拟分配(透传)
            return BudgetAllocation(
                allocation_id="disabled",
                scope=scope,
                scope_id=scope_id,
                dimension=dimension,
                amount=amount,
                consumer=consumer,
                request_id=request_id,
            )

        with self._lock:
            self._load()
            # 检查所有父域是否超限
            chain = self._scope_chain(scope, scope_id)
            for s, sid in chain:
                used = self._usage[s].get(sid, {}).get(dimension, 0)
                limit = self._get_limit(s, sid, dimension)
                if limit > 0 and used + amount > limit:
                    # 超限
                    if strict:
                        raise BudgetExceededError(s, dimension, used, limit, consumer)
                    logger.warning(
                        "Budget exceeded(scope=%s/%s, dim=%s, used=%d/%d, consumer=%s) - degrading",
                        s.value, sid, dimension.value, used, limit, consumer,
                    )
                    return None  # 返回 None 表示拒绝分配(调用方降级)

            # 全部通过 → 扣减
            alloc_id = self._generate_id(scope, scope_id, dimension, amount)
            alloc = BudgetAllocation(
                allocation_id=alloc_id,
                scope=scope,
                scope_id=scope_id,
                dimension=dimension,
                amount=amount,
                consumer=consumer,
                request_id=request_id,
            )
            self._allocations[alloc_id] = alloc

            # 累计扣减(预扣)
            for s, sid in chain:
                self._usage[s].setdefault(sid, {}).setdefault(dimension, 0)
                self._usage[s][sid][dimension] += amount
            self._save()
        return alloc

    def release(
        self,
        allocation_id: str,
        actual_used: Optional[int] = None,
    ) -> bool:
        """释放预分配(回退未用部分)。

        Args:
            allocation_id: allocate 返回的 ID
            actual_used: 实际使用量(<= amount),None 则按 amount 全计
        """
        if not is_enabled("defense"):
            return True

        with self._lock:
            self._load()
            alloc = self._allocations.get(allocation_id)
            if alloc is None or alloc.released:
                return False

            used = actual_used if actual_used is not None else alloc.amount
            used = min(used, alloc.amount)
            refund = alloc.amount - used

            # 回退未用部分
            if refund > 0:
                chain = self._scope_chain(alloc.scope, alloc.scope_id)
                for s, sid in chain:
                    self._usage[s].setdefault(sid, {}).setdefault(alloc.dimension, 0)
                    self._usage[s][sid][alloc.dimension] = max(
                        0, self._usage[s][sid][alloc.dimension] - refund,
                    )

            alloc.released = True
            alloc.released_at = time.time()
            alloc.actual_used = used
            self._save()
        return True

    def check(
        self,
        scope: BudgetScope,
        scope_id: str,
        dimension: BudgetDimension,
    ) -> dict[str, Any]:
        """查询 budget 状态(不扣减)。"""
        if not is_enabled("defense"):
            return {"used": 0, "limit": 10**9, "remaining": 10**9, "utilization": 0.0}

        with self._lock:
            self._load()
            used = self._usage[scope].get(scope_id, {}).get(dimension, 0)
            limit = self._get_limit(scope, scope_id, dimension)
            remaining = max(0, limit - used) if limit > 0 else -1
            util = used / limit if limit > 0 else 0.0
            return {
                "scope": scope.value,
                "scope_id": scope_id,
                "dimension": dimension.value,
                "used": used,
                "limit": limit,
                "remaining": remaining,
                "utilization": util,
            }

    def set_limit(
        self,
        scope: BudgetScope,
        dimension: BudgetDimension,
        limit: int,
        scope_id: Optional[str] = None,  # 仅 TENANT / USER 级别生效
    ) -> None:
        """设置 / 覆盖 limit(管理用)。

        当 scope_id 不为空且 scope 为 TENANT/USER 时，存储 per-entity 覆盖值，
        使不同租户/用户可拥有独立限额。
        """
        with self._lock:
            if scope_id and scope in (BudgetScope.TENANT, BudgetScope.USER):
                # per-entity 覆盖：存入 _scope_overrides
                override_key = (scope.value, scope_id)
                if override_key not in self._scope_overrides:
                    self._scope_overrides[override_key] = {}
                self._scope_overrides[override_key][dimension] = limit
            else:
                # 全局 scope 级默认值
                self._limits[scope][dimension] = limit
            self._save()

    def reset_session(self, session_id: str) -> None:
        """会话结束时清理 session 级 budget(避免内存泄漏)。"""
        with self._lock:
            self._usage[BudgetScope.SESSION].pop(session_id, None)
            # 清理该 session 的 allocations
            to_remove = [
                aid for aid, alloc in self._allocations.items()
                if alloc.scope == BudgetScope.SESSION and alloc.scope_id == session_id
            ]
            for aid in to_remove:
                self._allocations.pop(aid, None)
            self._save()

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_limit(
        self,
        scope: BudgetScope,
        scope_id: str,
        dimension: BudgetDimension,
    ) -> int:
        """获取有效 limit：优先 per-entity 覆盖，否则用 scope 级默认值。"""
        override_key = (scope.value, scope_id)
        override = self._scope_overrides.get(override_key)
        if override and dimension in override:
            return override[dimension]
        return self._limits[scope].get(dimension, 0)

    def _scope_chain(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> list[tuple[BudgetScope, str]]:
        """返回从指定 scope 到根的所有祖先 scope。

        例:USER scope → [USER, TENANT, GLOBAL]
        """
        # 从当前 scope 出发,逐级向上
        chain: list[tuple[BudgetScope, str]] = []
        if scope == BudgetScope.SESSION:
            chain.append((BudgetScope.SESSION, scope_id))
            # session → user(假设 scope_id 含 user 信息)
            # 简化:session_id 格式 "session-{user_id}-{uuid}"
            user_id = self._extract_user_from_session(scope_id)
            chain.append((BudgetScope.USER, user_id))
            tid = get_current_tenant_id()
            chain.append((BudgetScope.TENANT, tid))
            chain.append((BudgetScope.GLOBAL, "global"))
        elif scope == BudgetScope.USER:
            chain.append((BudgetScope.USER, scope_id))
            tid = get_current_tenant_id()
            chain.append((BudgetScope.TENANT, tid))
            chain.append((BudgetScope.GLOBAL, "global"))
        elif scope == BudgetScope.TENANT:
            chain.append((BudgetScope.TENANT, scope_id))
            chain.append((BudgetScope.GLOBAL, "global"))
        else:  # GLOBAL
            chain.append((BudgetScope.GLOBAL, "global"))
        return chain

    def _extract_user_from_session(self, session_id: str) -> str:
        """从 session_id 提取 user_id(格式约定:session-<user_id>-<uuid>)。"""
        parts = session_id.split("-", 2)
        if len(parts) >= 2:
            return parts[1]
        return "unknown"

    def _generate_id(
        self,
        scope: BudgetScope,
        scope_id: str,
        dimension: BudgetDimension,
        amount: int,
    ) -> str:
        ts = time.time()
        h = hashlib.sha256(
            f"{scope.value}:{scope_id}:{dimension.value}:{amount}:{ts}".encode(),
        ).hexdigest()[:12]
        return f"alloc-{h}"

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                # 加载 usage
                for scope_str, scope_data in data.get("usage", {}).items():
                    scope = BudgetScope(scope_str)
                    self._usage[scope] = {}
                    for sid, dims in scope_data.items():
                        self._usage[scope][sid] = {
                            BudgetDimension(d): v for d, v in dims.items()
                        }
                # 加载 allocations
                self._allocations = {
                    aid: BudgetAllocation(
                        allocation_id=a["allocation_id"],
                        scope=BudgetScope(a["scope"]),
                        scope_id=a["scope_id"],
                        dimension=BudgetDimension(a["dimension"]),
                        amount=a["amount"],
                        consumer=a["consumer"],
                        request_id=a.get("request_id", ""),
                        timestamp=a.get("timestamp", time.time()),
                        released=a.get("released", False),
                        released_at=a.get("released_at"),
                        actual_used=a.get("actual_used", 0),
                    )
                    for aid, a in data.get("allocations", {}).items()
                }
                # 加载 limits(若文件中有,优先用文件中的)
                stored_limits = data.get("limits", {})
                for scope_str, dim_limits in stored_limits.items():
                    scope = BudgetScope(scope_str)
                    self._limits[scope] = {
                        BudgetDimension(d): v for d, v in dim_limits.items()
                    }
                # 加载 per-entity 覆盖
                for key_str, dim_limits in data.get("scope_overrides", {}).items():
                    # key_str 格式: "scope:scope_id"
                    parts = key_str.split(":", 1)
                    if len(parts) == 2:
                        self._scope_overrides[(parts[0], parts[1])] = {
                            BudgetDimension(d): v for d, v in dim_limits.items()
                        }
        except Exception as e:
            logger.warning("Load budget store failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            data = {
                "usage": {
                    scope.value: {
                        sid: {d.value: v for d, v in dims.items()}
                        for sid, dims in scope_data.items()
                    }
                    for scope, scope_data in self._usage.items()
                },
                "allocations": {
                    aid: a.to_dict() for aid, a in self._allocations.items()
                },
                "limits": {
                    scope.value: {d.value: v for d, v in dims.items()}
                    for scope, dims in self._limits.items()
                },
                "scope_overrides": {
                    f"{scope_val}:{sid}": {d.value: v for d, v in dims.items()}
                    for (scope_val, sid), dims in self._scope_overrides.items()
                },
                "updated_at": time.time(),
            }
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Save budget store failed: %s", e)


# 全局单例
_bc_instance: Optional[BudgetCoordinator] = None
_bc_lock = threading.Lock()


def get_budget_coordinator() -> BudgetCoordinator:
    global _bc_instance
    if _bc_instance is None:
        with _bc_lock:
            if _bc_instance is None:
                _bc_instance = BudgetCoordinator()
    return _bc_instance
