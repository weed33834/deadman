"""P8.1.4 用量追踪 - 实时计数 + 配额检查(与 P7.7 quota 协同)。

设计:
    - UsageTracker 是 metering 的上层封装,提供"实时查询 + 配额检查"
    - 与 P7.7 QuotaManager 协同:
        - metering 记原始事件(append-only)
        - quota 用滑动窗口计数(实时)
        - UsageTracker 整合两者,业务层只调一次
    - 提供"配额预测性告警"(D7.19 防御性工程)

接口:
    - record_token / record_tool_call / record_storage / record_multimodal
    - get_usage(user_id, period) → UsageReport
    - check_quota(user_id, dimension) → QuotaResult
    - predict_overflow(user_id, dimension) → Optional[float]  # 预测超限时间
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id
from ..infrastructure.quota import (
    QuotaAction,
    QuotaExceededError,
    QuotaManager,
    QuotaUsage,
    get_quota_manager,
)
from .metering import MeteringDimension, MeteringService, get_metering_service
from .subscription import SubscriptionManager, get_subscription_manager

logger = logging.getLogger(__name__)


@dataclass
class UsageReport:
    """用量报告(看板 / API 返回用)。"""

    user_id: str
    period: str  # "YYYY-MM-DD" / "YYYY-MM"
    llm_tokens: int = 0
    tool_calls: int = 0
    storage_mb: int = 0
    multimodal_calls: int = 0
    # 详细分布(便于可视化)
    by_model: dict[str, int] = field(default_factory=dict)  # model → tokens
    by_tool: dict[str, int] = field(default_factory=dict)  # tool_name → count
    by_multimodal_type: dict[str, int] = field(default_factory=dict)


@dataclass
class QuotaResult:
    """配额检查结果。"""

    dimension: str
    used: int
    limit: int
    remaining: int
    utilization: float  # 0-1
    triggered_actions: list[str] = field(default_factory=list)
    will_exceed: bool = False  # 是否即将超限(预测)
    predicted_overflow_at: Optional[float] = None  # epoch(若预测超限)


class UsageTracker:
    """用量追踪器 - 整合 metering + quota + subscription。"""

    def __init__(
        self,
        metering: Optional[MeteringService] = None,
        quota: Optional[QuotaManager] = None,
        subscriptions: Optional[SubscriptionManager] = None,
    ) -> None:
        self.metering = metering or get_metering_service()
        self.quota = quota or get_quota_manager()
        self.subscriptions = subscriptions or get_subscription_manager()
        self._lock = threading.RLock()
        # 预测缓存:{(user_id, dimension): (timestamp, used_at_timestamp)}
        self._predict_cache: dict[tuple[str, str], tuple[float, int]] = {}

    # ==================================================================
    # 记录用量(同时触发计量 + 配额消费)
    # ==================================================================

    def record_token(
        self,
        user_id: str,
        tokens: int,
        model: str = "",
        tenant_id: Optional[str] = None,
    ) -> QuotaResult:
        """记录 LLM token 用量。"""
        # 1. metering 记原始事件
        self.metering.record_llm_tokens(user_id, tokens, model=model, tenant_id=tenant_id)

        # 2. quota 消费配额
        return self._consume_quota("llm_tokens", tokens, user_id, tenant_id)

    def record_tool_call(
        self,
        user_id: str,
        tool_name: str = "",
        tenant_id: Optional[str] = None,
    ) -> QuotaResult:
        """记录工具调用。"""
        self.metering.record_tool_call(user_id, tool_name=tool_name, tenant_id=tenant_id)
        return self._consume_quota("tool_calls", 1, user_id, tenant_id)

    def record_storage(
        self,
        user_id: str,
        bytes_: int,
        tenant_id: Optional[str] = None,
    ) -> QuotaResult:
        """记录存储使用(累计)。"""
        self.metering.record_storage(user_id, bytes_, tenant_id=tenant_id)
        mb = (bytes_ + 1024 * 1024 - 1) // (1024 * 1024)
        return self._consume_quota("storage_mb", mb, user_id, tenant_id)

    def record_multimodal(
        self,
        user_id: str,
        multimodal_type: str,
        tenant_id: Optional[str] = None,
    ) -> QuotaResult:
        """记录多模态调用。"""
        self.metering.record_multimodal(user_id, multimodal_type, tenant_id=tenant_id)
        return self._consume_quota("multimodal_calls", 1, user_id, tenant_id)

    # ==================================================================
    # 查询用量
    # ==================================================================

    def get_usage(self, user_id: str, period: Optional[str] = None) -> UsageReport:
        """获取用量报告。

        Args:
            period: "YYYY-MM-DD" / "YYYY-MM" / None(默认当天)
        """
        if period is None:
            period = time.strftime("%Y-%m-%d", time.localtime())

        if len(period) == 10:  # YYYY-MM-DD
            usage_dict = self.metering.get_daily_usage(user_id, period)
        elif len(period) == 7:  # YYYY-MM
            usage_dict = self.metering.get_monthly_usage(user_id, period)
        else:
            usage_dict = {d.value: 0 for d in MeteringDimension}

        return UsageReport(
            user_id=user_id,
            period=period,
            llm_tokens=usage_dict.get(MeteringDimension.LLM_TOKENS.value, 0),
            tool_calls=usage_dict.get(MeteringDimension.TOOL_CALLS.value, 0),
            storage_mb=usage_dict.get(MeteringDimension.STORAGE.value, 0),
            multimodal_calls=usage_dict.get(MeteringDimension.MULTIMODAL.value, 0),
        )

    def get_current_usage(self, user_id: str) -> list[QuotaUsage]:
        """获取当前配额快照(滑动窗口)。"""
        if not is_enabled("billing"):
            return []
        return self.quota.list_usage(get_current_tenant_id())

    # ==================================================================
    # 配额检查 + 预测
    # ==================================================================

    def check_quota(
        self,
        user_id: str,
        dimension: str,
        tenant_id: Optional[str] = None,
    ) -> QuotaResult:
        """查配额(不消费,仅查)。"""
        if not is_enabled("billing"):
            return QuotaResult(
                dimension=dimension,
                used=0,
                limit=10**9,
                remaining=10**9,
                utilization=0.0,
            )

        tid = tenant_id or get_current_tenant_id()
        usage = self.quota.check(dimension, tid)

        # 预测
        will_exceed, predicted_at = self._predict_overflow(user_id, dimension, usage, tid)

        return QuotaResult(
            dimension=dimension,
            used=usage.used,
            limit=usage.limit,
            remaining=usage.remaining,
            utilization=usage.utilization,
            triggered_actions=[a.value for a in usage.triggered_actions],
            will_exceed=will_exceed,
            predicted_overflow_at=predicted_at,
        )

    def predict_overflow(
        self,
        user_id: str,
        dimension: str,
        tenant_id: Optional[str] = None,
    ) -> Optional[float]:
        """预测某维度何时超限(基于最近速率)。

        Returns:
            预测超限的 epoch(若预计会超限);None 若不会超限。
        """
        result = self.check_quota(user_id, dimension, tenant_id)
        return result.predicted_overflow_at

    # ==================================================================
    # 内部
    # ==================================================================

    def _consume_quota(
        self,
        dimension: str,
        amount: int,
        user_id: str,
        tenant_id: Optional[str],
    ) -> QuotaResult:
        """消费配额,返回配额检查结果。"""
        if not is_enabled("billing"):
            return QuotaResult(
                dimension=dimension,
                used=0,
                limit=10**9,
                remaining=10**9,
                utilization=0.0,
            )

        tid = tenant_id or get_current_tenant_id()
        try:
            usage = self.quota.consume(dimension, amount, tid)
        except QuotaExceededError as e:
            # 超限拒绝:返回明确状态,不抛
            logger.warning("Quota rejected: %s (user=%s, used=%d/%d)", dimension, user_id, e.used, e.limit)
            return QuotaResult(
                dimension=dimension,
                used=e.used,
                limit=e.limit,
                remaining=0,
                utilization=1.0,
                triggered_actions=[QuotaAction.REJECT.value],
                will_exceed=True,
            )

        will_exceed, predicted_at = self._predict_overflow(user_id, dimension, usage, tid)
        return QuotaResult(
            dimension=dimension,
            used=usage.used,
            limit=usage.limit,
            remaining=usage.remaining,
            utilization=usage.utilization,
            triggered_actions=[a.value for a in usage.triggered_actions],
            will_exceed=will_exceed,
            predicted_overflow_at=predicted_at,
        )

    def _predict_overflow(
        self,
        user_id: str,
        dimension: str,
        usage: QuotaUsage,
        tenant_id: str,
    ) -> tuple[bool, Optional[float]]:
        """预测是否超限(基于最近 N 分钟速率)。

        算法:
            - 取过去 5 分钟用量
            - 计算速率(单位 / 秒)
            - 按速率推算到达 limit 的时间
            - 若在 reset_at 之前到达 → 预测超限
        """
        if usage.limit <= 0 or usage.remaining <= 0:
            return False, None

        # 取最近 5 分钟的累计
        now = time.time()
        five_min_ago = now - 300
        recent_used = self._estimate_recent_usage(user_id, dimension, five_min_ago, now)
        if recent_used <= 0:
            return False, None

        # 速率(单位 / 秒)
        rate = recent_used / 300
        if rate <= 0:
            return False, None

        # 预测超限时间
        time_to_overflow = usage.remaining / rate
        predicted_at = now + time_to_overflow

        # 若超限时间早于 reset_at → 会超限
        if predicted_at < usage.reset_at:
            # 缓存预测
            self._predict_cache[(user_id, dimension)] = (now, recent_used)
            return True, predicted_at
        return False, None

    def _estimate_recent_usage(
        self,
        user_id: str,
        dimension: str,
        start_ts: float,
        end_ts: float,
    ) -> int:
        """估算最近时间段内的用量(简化版:从 metering 读当日文件)。"""
        # 简化实现:当日 metering 总量 / (now - 当日 0 点) * 5min
        # 完整实现需要按 timestamp 区间扫描(后续优化)
        try:
            date_str = time.strftime("%Y-%m-%d", time.localtime(end_ts))
            daily = self.metering.get_daily_usage(user_id, date_str)
            dim_key = dimension
            if dimension == "llm_tokens":
                dim_key = MeteringDimension.LLM_TOKENS.value
            elif dimension == "tool_calls":
                dim_key = MeteringDimension.TOOL_CALLS.value
            elif dimension == "storage_mb":
                dim_key = MeteringDimension.STORAGE.value
            elif dimension == "multimodal_calls":
                dim_key = MeteringDimension.MULTIMODAL.value
            daily_total = daily.get(dim_key, 0)

            # 当日 0 点到现在的时间
            today_start = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            elapsed = end_ts - today_start
            if elapsed <= 0:
                return 0
            # 估算 5 分钟内的量
            return int(daily_total * 300 / elapsed)
        except Exception as e:
            logger.debug("Estimate recent usage failed: %s", e)
            return 0


# 全局单例
_ut_instance: Optional[UsageTracker] = None
_ut_lock = threading.Lock()


def get_usage_tracker() -> UsageTracker:
    global _ut_instance
    if _ut_instance is None:
        with _ut_lock:
            if _ut_instance is None:
                _ut_instance = UsageTracker()
    return _ut_instance
