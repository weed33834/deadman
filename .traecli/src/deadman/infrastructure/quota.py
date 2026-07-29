"""P7.7 配额与计费 - 多维度配额管理 + 超限降级。

借鉴 Stripe / OpenAI API 的配额实践:
    - 多维度配额:token 数 / 工具调用次数 / 存储大小 / API 请求频率
    - 按租户/用户独立配额(与 multi_tenant.py 协同)
    - 时间窗口:每分钟 / 每天 / 每月
    - 超限策略(优先降级,不直接拒绝):
        1. 限速(rate_limit):降低到 50% 速率
        2. 降级模型:从 gpt-4o 切到 gpt-4o-mini
        3. 限功能:禁用昂贵工具(如 debate)
        4. 软警告:仅日志,继续放行(用于免费用户)
        5. 拒绝(最后手段):返回 429

    - 滑动窗口计数(基于 Redis Sorted Set 模式,但用文件实现)
    - 实时统计 + 预测:预计 N 分钟后超额 → 提前降级

feature flag:`DEADMAN_QUOTA_ENABLED=0` 默认关闭。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .feature_flags import is_enabled
from .multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


class QuotaPeriod(str, Enum):
    """配额时间窗口。"""

    PER_MINUTE = "minute"
    PER_HOUR = "hour"
    PER_DAY = "day"
    PER_MONTH = "month"


class QuotaAction(str, Enum):
    """超限动作(优先级 1→5,从轻到重)。"""

    WARN = "warn"  # 仅日志,继续放行
    RATE_LIMIT = "rate_limit"  # 限速到 50%
    DOWNGRADE_MODEL = "downgrade_model"  # 切便宜模型
    DISABLE_FEATURE = "disable_feature"  # 禁用昂贵工具
    REJECT = "reject"  # 拒绝(429)


@dataclass
class QuotaLimit:
    """单个配额限制。"""

    name: str  # 配额名(如 "llm_tokens")
    period: QuotaPeriod
    limit: int  # 配额上限
    actions: list[QuotaAction] = field(default_factory=list)
    # 各动作的触发阈值(0-1,占 limit 的比例)
    # 例如:0.8 → 达到 80% 配额时触发对应 action
    thresholds: dict[str, float] = field(default_factory=dict)


@dataclass
class QuotaUsage:
    """当前用量快照。"""

    name: str
    period: QuotaPeriod
    limit: int
    used: int
    remaining: int
    utilization: float  # used / limit
    triggered_actions: list[QuotaAction] = field(default_factory=list)
    reset_at: float = 0.0  # 配额重置时间


class QuotaExceededError(Exception):
    """配额超限(且 action=REJECT 时抛出)。"""

    def __init__(self, name: str, used: int, limit: int) -> None:
        self.name = name
        self.used = used
        self.limit = limit
        super().__init__(f"Quota '{name}' exceeded: {used}/{limit}")


# =====================================================================
# 滑动窗口计数器
# =====================================================================

class SlidingWindowCounter:
    """滑动窗口计数器(基于时间分桶)。

    原理:把窗口分成 N 个 bucket,每个 bucket 记录一段时间的累计。
    查询时:sum(在窗口内的 buckets) - sum(过期 buckets)。

    持久化到 JSON 文件,重启不丢配额。
    """

    def __init__(self, window_seconds: int, bucket_count: int = 12) -> None:
        """Args:
            window_seconds: 窗口总时长(秒)
            bucket_count: 分桶数(默认 12,即每桶 1/12 窗口时长)
        """
        self.window_seconds = window_seconds
        self.bucket_count = bucket_count
        self.bucket_size = window_seconds // bucket_count
        # buckets: {bucket_start_timestamp: count}
        self._buckets: dict[int, int] = {}
        self._lock = threading.RLock()

    def add(self, count: int = 1, now: Optional[float] = None) -> int:
        """增加计数,返回当前窗口总数。"""
        now = now or time.time()
        bucket_start = int(now // self.bucket_size) * self.bucket_size
        with self._lock:
            self._evict_expired(now)
            self._buckets[bucket_start] = self._buckets.get(bucket_start, 0) + count
            return self.current(now)

    def current(self, now: Optional[float] = None) -> int:
        """查询当前窗口总数。"""
        now = now or time.time()
        with self._lock:
            self._evict_expired(now)
            return sum(self._buckets.values())

    def reset_at(self, now: Optional[float] = None) -> float:
        """返回最早 bucket 过期时间(配额重置时间)。"""
        now = now or time.time()
        with self._lock:
            if not self._buckets:
                return now + self.window_seconds
            oldest = min(self._buckets.keys())
            return oldest + self.window_seconds

    def _evict_expired(self, now: float) -> None:
        """清除过期 buckets。"""
        cutoff = now - self.window_seconds
        expired = [ts for ts in self._buckets.keys() if ts < cutoff]
        for ts in expired:
            del self._buckets[ts]

    def to_dict(self) -> dict:
        return {"window_seconds": self.window_seconds, "buckets": dict(self._buckets)}

    @classmethod
    def from_dict(cls, data: dict) -> "SlidingWindowCounter":
        c = cls(window_seconds=data["window_seconds"])
        c._buckets = {int(k): int(v) for k, v in data.get("buckets", {}).items()}
        return c


# =====================================================================
# 配额管理器
# =====================================================================

PERIOD_SECONDS = {
    QuotaPeriod.PER_MINUTE: 60,
    QuotaPeriod.PER_HOUR: 3600,
    QuotaPeriod.PER_DAY: 86400,
    QuotaPeriod.PER_MONTH: 86400 * 30,
}


class QuotaManager:
    """配额管理器 - 按 tenant_id + quota_name 维度计数。"""

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_QUOTA_STORE", "data/quota.json")
        )
        self._lock = threading.RLock()
        # {tenant_id: {quota_name: SlidingWindowCounter}}
        self._counters: dict[str, dict[str, SlidingWindowCounter]] = {}
        # 默认配额限制(可被 tenant-level 覆盖)
        self._default_limits: dict[str, QuotaLimit] = self._init_default_limits()
        self._loaded = False

    def _init_default_limits(self) -> dict[str, QuotaLimit]:
        """默认配额(免费 plan)。"""
        return {
            "llm_tokens": QuotaLimit(
                name="llm_tokens",
                period=QuotaPeriod.PER_DAY,
                limit=100_000,
                actions=[QuotaAction.WARN, QuotaAction.DOWNGRADE_MODEL, QuotaAction.RATE_LIMIT, QuotaAction.REJECT],
                thresholds={"warn": 0.8, "downgrade_model": 0.9, "rate_limit": 0.95, "reject": 1.0},
            ),
            "tool_calls": QuotaLimit(
                name="tool_calls",
                period=QuotaPeriod.PER_DAY,
                limit=1_000,
                actions=[QuotaAction.WARN, QuotaAction.DISABLE_FEATURE, QuotaAction.REJECT],
                thresholds={"warn": 0.8, "disable_feature": 0.95, "reject": 1.0},
            ),
            "storage_mb": QuotaLimit(
                name="storage_mb",
                period=QuotaPeriod.PER_MONTH,
                limit=100,
                actions=[QuotaAction.WARN, QuotaAction.REJECT],
                thresholds={"warn": 0.9, "reject": 1.0},
            ),
            "api_requests": QuotaLimit(
                name="api_requests",
                period=QuotaPeriod.PER_MINUTE,
                limit=60,
                actions=[QuotaAction.WARN, QuotaAction.RATE_LIMIT, QuotaAction.REJECT],
                thresholds={"warn": 0.8, "rate_limit": 0.9, "reject": 1.0},
            ),
        }

    # ==================================================================
    # 消费配额
    # ==================================================================

    def consume(
        self,
        quota_name: str,
        amount: int = 1,
        tenant_id: Optional[str] = None,
    ) -> QuotaUsage:
        """消费配额。

        Returns:
            QuotaUsage 当前用量

        Raises:
            QuotaExceededError: 配额超限且 action=REJECT
        """
        if not is_enabled("quota"):
            # 关闭:返回虚拟的"无限配额"
            return QuotaUsage(
                name=quota_name,
                period=QuotaPeriod.PER_DAY,
                limit=10**9,
                used=0,
                remaining=10**9,
                utilization=0.0,
            )

        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            counter = self._get_counter(tid, quota_name)
            current = counter.add(amount)

            limit = self._get_limit(tid, quota_name)
            usage = QuotaUsage(
                name=quota_name,
                period=limit.period,
                limit=limit.limit,
                used=current,
                remaining=max(0, limit.limit - current),
                utilization=current / limit.limit if limit.limit > 0 else 0.0,
                reset_at=counter.reset_at(),
            )

            # 触发超限动作
            triggered = self._check_thresholds(usage, limit)
            usage.triggered_actions = triggered

            if QuotaAction.REJECT in triggered and current > limit.limit:
                logger.warning(
                    "Quota %s exceeded for tenant %s: %d/%d",
                    quota_name,
                    tid,
                    current,
                    limit.limit,
                )
                # 持久化
                self._save()
                raise QuotaExceededError(quota_name, current, limit.limit)

            # 持久化(每消费一次都保存,确保不丢)
            self._save()
            return usage

    def check(self, quota_name: str, tenant_id: Optional[str] = None) -> QuotaUsage:
        """查询当前用量(不消费)。"""
        if not is_enabled("quota"):
            return QuotaUsage(
                name=quota_name,
                period=QuotaPeriod.PER_DAY,
                limit=10**9,
                used=0,
                remaining=10**9,
                utilization=0.0,
            )

        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            counter = self._get_counter(tid, quota_name)
            current = counter.current()
            limit = self._get_limit(tid, quota_name)
            usage = QuotaUsage(
                name=quota_name,
                period=limit.period,
                limit=limit.limit,
                used=current,
                remaining=max(0, limit.limit - current),
                utilization=current / limit.limit if limit.limit > 0 else 0.0,
                reset_at=counter.reset_at(),
            )
            usage.triggered_actions = self._check_thresholds(usage, limit)
            return usage

    def set_tenant_limit(
        self,
        tenant_id: str,
        quota_name: str,
        limit: int,
        period: Optional[QuotaPeriod] = None,
    ) -> None:
        """为指定租户设置自定义配额(覆盖默认)。"""
        with self._lock:
            self._load()
            # 存到 _tenant_limits
            self._tenant_limits.setdefault(tenant_id, {})[quota_name] = QuotaLimit(
                name=quota_name,
                period=period or self._default_limits[quota_name].period,
                limit=limit,
                actions=self._default_limits[quota_name].actions,
                thresholds=self._default_limits[quota_name].thresholds,
            )
            self._save()

    def reset(self, tenant_id: Optional[str] = None, quota_name: Optional[str] = None) -> None:
        """重置配额(运维用)。"""
        with self._lock:
            if tenant_id is None:
                self._counters.clear()
            elif quota_name is None:
                self._counters.pop(tenant_id, None)
            else:
                self._counters.get(tenant_id, {}).pop(quota_name, None)
            self._save()

    def list_usage(self, tenant_id: Optional[str] = None) -> list[QuotaUsage]:
        """列出所有配额用量(看板用)。"""
        tid = tenant_id or get_current_tenant_id()
        result: list[QuotaUsage] = []
        for name in self._default_limits.keys():
            result.append(self.check(name, tid))
        return result

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_counter(self, tenant_id: str, quota_name: str) -> SlidingWindowCounter:
        """获取或创建计数器。"""
        if quota_name not in self._default_limits:
            # 未知配额名,返回默认 1 天窗口
            limit_obj = QuotaLimit(name=quota_name, period=QuotaPeriod.PER_DAY, limit=10**6)
            self._default_limits[quota_name] = limit_obj

        tenant_counters = self._counters.setdefault(tenant_id, {})
        if quota_name not in tenant_counters:
            limit = self._default_limits[quota_name]
            tenant_counters[quota_name] = SlidingWindowCounter(
                window_seconds=PERIOD_SECONDS[limit.period]
            )
        return tenant_counters[quota_name]

    def _get_limit(self, tenant_id: str, quota_name: str) -> QuotaLimit:
        """获取配额限制(优先 tenant 自定义,其次默认)。"""
        tenant_overrides = getattr(self, "_tenant_limits", {}).get(tenant_id, {})
        if quota_name in tenant_overrides:
            return tenant_overrides[quota_name]
        return self._default_limits[quota_name]

    def _check_thresholds(
        self,
        usage: QuotaUsage,
        limit: QuotaLimit,
    ) -> list[QuotaAction]:
        """检查哪些阈值被触发。"""
        triggered: list[QuotaAction] = []
        for action in limit.actions:
            threshold = limit.thresholds.get(action.value, 1.0)
            if usage.utilization >= threshold:
                triggered.append(action)
                logger.info(
                    "Quota %s action triggered: %s at %.0f%% (used=%d/%d)",
                    usage.name,
                    action.value,
                    usage.utilization * 100,
                    usage.used,
                    usage.limit,
                )
        return triggered

    def _load(self) -> None:
        if self._loaded:
            return
        if not hasattr(self, "_tenant_limits"):
            self._tenant_limits = {}
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                # 加载 counters
                for tid, quotas in data.get("counters", {}).items():
                    self._counters[tid] = {}
                    for qname, cdata in quotas.items():
                        self._counters[tid][qname] = SlidingWindowCounter.from_dict(cdata)
                # 加载 tenant limits
                for tid, limits in data.get("tenant_limits", {}).items():
                    self._tenant_limits[tid] = {}
                    for qname, ldata in limits.items():
                        self._tenant_limits[tid][qname] = QuotaLimit(
                            name=qname,
                            period=QuotaPeriod(ldata["period"]),
                            limit=ldata["limit"],
                            actions=[QuotaAction(a) for a in ldata.get("actions", [])],
                            thresholds=ldata.get("thresholds", {}),
                        )
        except Exception as e:
            logger.warning("Quota store load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "counters": {
                    tid: {qname: c.to_dict() for qname, c in quotas.items()}
                    for tid, quotas in self._counters.items()
                },
                "tenant_limits": {
                    tid: {qname: asdict(l) for qname, l in limits.items()}
                    for tid, limits in getattr(self, "_tenant_limits", {}).items()
                },
            }
            # 序列化 Enum 为 value
            for tid, limits in data["tenant_limits"].items():
                for qname, l in limits.items():
                    l["period"] = l["period"].value if hasattr(l["period"], "value") else l["period"]
                    l["actions"] = [a.value if hasattr(a, "value") else a for a in l["actions"]]
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Quota store save failed: %s", e)


# 全局单例
_qm_instance: Optional[QuotaManager] = None
_qm_lock = threading.Lock()


def get_quota_manager() -> QuotaManager:
    global _qm_instance
    if _qm_instance is None:
        with _qm_lock:
            if _qm_instance is None:
                _qm_instance = QuotaManager()
    return _qm_instance
