"""P8.4.4 Revenue Share - 用量记录 + 分账 + payout(纯记账,无真实资金流)。

设计:
    - UsageRecord: 单次调用使用记录(call_count + tokens + 估算成本)
    - RevenueSplit: 单 agent 单周期的分账结果(platform_share + author_share)
    - PayoutRecord: 给 author 的单次打款记录

约束(重要):
    - 所有 payment / revenue / payout 仅记录,不涉及真实资金流。
    - 价格用 price_per_call(来自 AgentListing)× call_count 估算。
    - Platform share 默认 30%,Author share 默认 70%,可按 plan 配置。

持久化:
    - `data/marketplace/revenue.json`(原子写,tenant-aware via resolve_data_path)

feature flag: `DEADMAN_MARKETPLACE_ENABLED=0`(默认关闭)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path
from .registry import MarketplaceError

logger = logging.getLogger(__name__)


# =====================================================================
# 默认分账配置
# =====================================================================
DEFAULT_PLATFORM_SHARE = 0.30  # 30%
DEFAULT_AUTHOR_SHARE = 0.70  # 70%

# 按 plan 覆盖的 platform_share(pro / enterprise 优惠)
PLAN_PLATFORM_SHARE: dict[str, float] = {
    "free": 0.30,
    "pro": 0.20,  # pro author 平台少抽
    "enterprise": 0.10,  # enterprise author 平台最少抽
}


# =====================================================================
# 数据模型
# =====================================================================
@dataclass
class UsageRecord:
    """单次使用记录(一个 user 对一个 agent 的累计调用)。

    Attributes:
        record_id: 唯一 ID
        agent_id: 被调 agent
        user_id: 调用者
        call_count: 调用次数
        tokens: 累计 token 数
        cost: 估算成本(price_per_call × call_count)
        timestamp: epoch
    """

    record_id: str
    agent_id: str
    user_id: str
    call_count: int
    tokens: int
    cost: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageRecord:
        return cls(
            record_id=data["record_id"],
            agent_id=data["agent_id"],
            user_id=data["user_id"],
            call_count=int(data["call_count"]),
            tokens=int(data["tokens"]),
            cost=float(data["cost"]),
            timestamp=float(data.get("timestamp", time.time())),
        )


@dataclass
class RevenueSplit:
    """单 agent 单周期分账结果。

    Attributes:
        agent_id: 被调 agent
        author_id: 作者(收入归属)
        total_revenue: 周期内总营收
        platform_share: 平台分成金额
        author_share: 作者分成金额
        period_start / period_end: 周期 [start, end)
        platform_rate: 平台分成比例(0-1)
        call_count: 周期内总调用次数
    """

    agent_id: str
    author_id: str
    total_revenue: float
    platform_share: float
    author_share: float
    period_start: float
    period_end: float
    platform_rate: float = DEFAULT_PLATFORM_SHARE
    call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PayoutRecord:
    """给 author 的单次打款记录(纯记账)。

    Attributes:
        payout_id: 唯一 ID
        author_id: 收款人
        amount: 打款金额
        period: 周期标识(如 "2026-07")
        agent_ids: 本 payout 包含的 agent 列表
        created_at: epoch
        status: pending / paid / failed
    """

    payout_id: str
    author_id: str
    amount: float
    period: str
    agent_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PayoutRecord:
        return cls(
            payout_id=data["payout_id"],
            author_id=data["author_id"],
            amount=float(data["amount"]),
            period=data["period"],
            agent_ids=list(data.get("agent_ids", []) or []),
            created_at=float(data.get("created_at", time.time())),
            status=data.get("status", "pending"),
        )


# =====================================================================
# RevenueShare
# =====================================================================
class RevenueShare:
    """Revenue Share 管理器(纯记账)。

    线程安全: 单实例 RLock + 原子 os.replace。
    多租户: 通过 resolve_data_path 自动隔离。
    """

    DEFAULT_STORE_REL = "marketplace/revenue.json"

    def __init__(
        self,
        store_path: Any | None = None,
        # 可注入 registry 用于查 price_per_call / author
        registry: Any = None,
    ) -> None:
        self._explicit_store = store_path
        self._registry = registry
        self._lock = threading.RLock()
        self._usage: list[UsageRecord] = []
        self._payouts: dict[str, PayoutRecord] = {}  # payout_id → record
        # 当前 cache 对应的 store path(检测 tenant 切换)
        self._loaded_path: str | None = None

    # ==================================================================
    # 路径解析 + 注入
    # ==================================================================
    def _resolve_store_path(self):
        if self._explicit_store is not None:
            return self._explicit_store
        return resolve_data_path(self.DEFAULT_STORE_REL)

    def set_registry(self, registry: Any) -> None:
        """注入 MarketplaceRegistry(用于查 price_per_call / author_id)。"""
        self._registry = registry

    # ==================================================================
    # 用量记录
    # ==================================================================
    def record_usage(
        self,
        agent_id: str,
        user_id: str,
        call_count: int,
        tokens: int,
    ) -> UsageRecord:
        """记录单次使用(增量累加)。

        Args:
            agent_id: 被调 agent
            user_id: 调用者
            call_count: 本次调用次数
            tokens: 本次 token 数

        Returns:
            UsageRecord(本次记录)
        """
        self._require_enabled()
        with self._lock:
            self._load()
            now = time.time()
            price = self._lookup_price(agent_id)
            cost = price * call_count
            record_id = f"usage:{agent_id}:{user_id}:{int(now * 1000)}"
            # 同一 user 对同一 agent 已有记录则累加(便于分账)
            existing = next(
                (r for r in self._usage if r.agent_id == agent_id and r.user_id == user_id),
                None,
            )
            if existing is not None:
                existing.call_count += call_count
                existing.tokens += tokens
                existing.cost += cost
                existing.timestamp = now
                record = existing
            else:
                record = UsageRecord(
                    record_id=record_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    call_count=call_count,
                    tokens=tokens,
                    cost=cost,
                    timestamp=now,
                )
                self._usage.append(record)
            self._save()
            return record

    def get_usage(
        self,
        agent_id: str,
        period_start: float | None = None,
        period_end: float | None = None,
    ) -> list[UsageRecord]:
        """查询 agent 的用量(可按时间区间过滤)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            results: list[UsageRecord] = []
            for r in self._usage:
                if r.agent_id != agent_id:
                    continue
                if period_start is not None and r.timestamp < period_start:
                    continue
                if period_end is not None and r.timestamp >= period_end:
                    continue
                results.append(r)
            return results

    # ==================================================================
    # 分账计算
    # ==================================================================
    def calculate_revenue(
        self,
        agent_id: str,
        period_start: float,
        period_end: float,
        author_id: str | None = None,
        plan: str = "free",
    ) -> RevenueSplit:
        """计算单 agent 单周期的分账。

        Args:
            agent_id: 目标 agent
            period_start / period_end: 周期 [start, end)
            author_id: 显式指定作者(默认从 registry 查)
            plan: 作者的 plan(影响 platform_share)

        Returns:
            RevenueSplit
        """
        self._require_enabled()
        with self._lock:
            self._load()
            # 聚合周期内用量
            period_records = [
                r
                for r in self._usage
                if r.agent_id == agent_id and period_start <= r.timestamp < period_end
            ]
            total_revenue = sum(r.cost for r in period_records)
            call_count = sum(r.call_count for r in period_records)

            # 作者 ID 解析
            if author_id is None:
                author_id = self._lookup_author(agent_id) or "unknown"

            # 分账比例
            platform_rate = PLAN_PLATFORM_SHARE.get(plan, DEFAULT_PLATFORM_SHARE)
            platform_share = total_revenue * platform_rate
            author_share = total_revenue * (1.0 - platform_rate)

            return RevenueSplit(
                agent_id=agent_id,
                author_id=author_id,
                total_revenue=total_revenue,
                platform_share=platform_share,
                author_share=author_share,
                period_start=period_start,
                period_end=period_end,
                platform_rate=platform_rate,
                call_count=call_count,
            )

    # ==================================================================
    # Payout
    # ==================================================================
    def payout(
        self,
        author_id: str,
        period: str,
        plan: str = "free",
    ) -> PayoutRecord:
        """生成本周期 author 的 payout(聚合 author 名下所有 agent 的 author_share)。

        Args:
            author_id: 收款 author
            period: 周期标识(如 "2026-07")
            plan: author 的 plan(影响分账比例)

        Returns:
            PayoutRecord
        """
        self._require_enabled()
        with self._lock:
            self._load()
            # 找 author 名下所有 agent(通过 registry 反查)
            agent_ids = self._lookup_author_agents(author_id)
            # 周期解析(简化:period "YYYY-MM" → 当月起止)
            period_start, period_end = self._parse_period(period)
            total_amount = 0.0
            for agent_id in agent_ids:
                split = self.calculate_revenue(
                    agent_id=agent_id,
                    period_start=period_start,
                    period_end=period_end,
                    author_id=author_id,
                    plan=plan,
                )
                total_amount += split.author_share
            payout_id = f"payout:{author_id}:{period}"
            record = PayoutRecord(
                payout_id=payout_id,
                author_id=author_id,
                amount=total_amount,
                period=period,
                agent_ids=list(agent_ids),
                created_at=time.time(),
                status="pending",
            )
            self._payouts[payout_id] = record
            self._save()
            logger.info(
                "Payout generated: author=%s period=%s amount=%.2f agents=%d",
                author_id,
                period,
                total_amount,
                len(agent_ids),
            )
            return record

    def get_payouts(self, author_id: str | None = None) -> list[PayoutRecord]:
        """查询 payout 记录(可按 author 过滤)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            results = list(self._payouts.values())
            if author_id is not None:
                results = [p for p in results if p.author_id == author_id]
            results.sort(key=lambda p: p.created_at, reverse=True)
            return results

    # ==================================================================
    # 内部: 持久化 + lookup
    # ==================================================================
    def _lookup_price(self, agent_id: str) -> float:
        """从 registry 查 agent 的 price_per_call(失败返回 0)。"""
        if self._registry is None:
            return 0.0
        try:
            listing = self._registry.get(agent_id)
            if listing is not None:
                return float(listing.price_per_call or 0.0)
        except Exception as e:
            logger.debug("lookup price failed for %s: %s", agent_id, e)
        return 0.0

    def _lookup_author(self, agent_id: str) -> str | None:
        """从 registry 查 agent 的 author。"""
        if self._registry is None:
            return None
        try:
            listing = self._registry.get(agent_id)
            if listing is not None:
                return listing.author
        except Exception:
            return None
        return None

    def _lookup_author_agents(self, author_id: str) -> list[str]:
        """从 registry 反查 author 名下所有 agent_id。"""
        if self._registry is None:
            return []
        try:
            # 借 registry.list + 内部 cache(任意状态)
            # 简化: 直接访问 _cache(若可用)
            cache = getattr(self._registry, "_cache", {})
            if cache:
                return [aid for aid, listing in cache.items() if listing.author == author_id]
        except Exception as e:
            logger.debug("按作者查找列表失败: %s", e)
        return []

    @staticmethod
    def _parse_period(period: str) -> tuple[float, float]:
        """解析 "YYYY-MM" → (月初 epoch, 下月初 epoch)。

        格式不匹配时返回 (0, now + 30d)。
        """
        try:
            import datetime as _dt

            year, month = (int(x) for x in period.split("-"))
            start = _dt.datetime(year, month, 1, tzinfo=_dt.timezone.utc).timestamp()
            if month == 12:
                end = _dt.datetime(year + 1, 1, 1, tzinfo=_dt.timezone.utc).timestamp()
            else:
                end = _dt.datetime(year, month + 1, 1, tzinfo=_dt.timezone.utc).timestamp()
            return start, end
        except Exception:
            now = time.time()
            return 0.0, now + 30 * 86400

    def _load(self) -> None:
        store = self._resolve_store_path()
        store_key = str(store)
        if store_key == self._loaded_path:
            return
        try:
            new_usage: list[UsageRecord] = []
            new_payouts: dict[str, PayoutRecord] = {}
            if store.exists():
                text = store.read_text(encoding="utf-8")
                data = json.loads(text) if text.strip() else {}
                new_usage = [
                    UsageRecord.from_dict(rd)
                    for rd in (data.get("usage", []) or [])
                    if isinstance(rd, dict) and "record_id" in rd
                ]
                new_payouts = {
                    pid: PayoutRecord.from_dict(pd)
                    for pid, pd in (data.get("payouts", {}) or {}).items()
                    if isinstance(pd, dict) and "payout_id" in pd
                }
            self._usage = new_usage
            self._payouts = new_payouts
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Revenue store load failed (%s): %s", store, e)
            self._usage = []
            self._payouts = {}
        self._loaded_path = store_key

    def _save(self) -> None:
        store = self._resolve_store_path()
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "tenant_id": get_current_tenant_id(),
                "usage": [u.to_dict() for u in self._usage],
                "payouts": {pid: p.to_dict() for pid, p in self._payouts.items()},
            }
            tmp_path = store.with_suffix(store.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, store)
        except OSError as e:
            logger.error("Revenue store save failed (%s): %s", store, e)
            raise MarketplaceError(f"Revenue save failed: {e}") from e

    def _require_enabled(self) -> None:
        if not is_enabled("marketplace"):
            raise MarketplaceError(
                "Marketplace feature is disabled (set DEADMAN_MARKETPLACE_ENABLED=1)"
            )


# =====================================================================
# 全局单例
# =====================================================================
_revenue_instance: RevenueShare | None = None
_revenue_lock = threading.Lock()


def get_revenue_share() -> RevenueShare:
    """获取全局 RevenueShare 单例。"""
    global _revenue_instance
    if _revenue_instance is None:
        with _revenue_lock:
            if _revenue_instance is None:
                _revenue_instance = RevenueShare()
    return _revenue_instance
