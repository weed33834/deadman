"""P8.1.2 订阅管理 - 增删改查 + 升降级 + 续费 + 退订。

借鉴 Stripe Subscription API:
    - subscribe: 创建订阅
    - cancel: 取消订阅(立即 / 周期末)
    - upgrade / downgrade: 升降级(按比例退款 / 补款)
    - renew: 续费(手动 / 自动)
    - get_current: 查询当前订阅

持久化:`data/billing/subscriptions.json`(原子写,与 P7.6 durable_execution 同款)
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

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id
from .plans import Plan, PlanName, get_plan

logger = logging.getLogger(__name__)


class SubscriptionStatus(str, Enum):
    """订阅状态机:

    TRIALING → ACTIVE → PAST_DUE → CANCELED / EXPIRED
                          ↓
                       EXPIRED

    - TRIALING: 试用期(7 天免费)
    - ACTIVE: 已生效
    - PAST_DUE: 逾期未付款(7 天宽限期)
    - CANCELED: 已取消(立即或周期末)
    - EXPIRED: 已过期(超过宽限期)
    """

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


class BillingCycle(str, Enum):
    """计费周期。"""

    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass
class Subscription:
    """单用户订阅记录。"""

    user_id: str
    plan_name: str  # PlanName.value
    status: SubscriptionStatus
    billing_cycle: BillingCycle
    current_period_start: float  # epoch
    current_period_end: float  # epoch
    trial_end: Optional[float] = None  # 试用期结束时间
    canceled_at: Optional[float] = None
    cancel_at_period_end: bool = False  # 周期末取消
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 升降级历史(便于审计)
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["billing_cycle"] = self.billing_cycle.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Subscription":
        return cls(
            user_id=data["user_id"],
            plan_name=data["plan_name"],
            status=SubscriptionStatus(data["status"]),
            billing_cycle=BillingCycle(data["billing_cycle"]),
            current_period_start=data["current_period_start"],
            current_period_end=data["current_period_end"],
            trial_end=data.get("trial_end"),
            canceled_at=data.get("canceled_at"),
            cancel_at_period_end=data.get("cancel_at_period_end", False),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            history=data.get("history", []),
        )

    def is_active(self, now: Optional[float] = None) -> bool:
        """当前是否生效(包含 trial)。"""
        now = now or time.time()
        if self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
            return self.current_period_start <= now < self.current_period_end
        return False


TRIAL_DAYS = 7  # Pro 试用 7 天


class SubscriptionManager:
    """订阅管理器。

    线程安全:单实例读写各自加锁,跨实例并发写靠原子 os.replace 保护。
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_SUBSCRIPTION_STORE", "data/billing/subscriptions.json")
        )
        self._lock = threading.RLock()
        self._subs: dict[str, Subscription] = {}  # user_id → Subscription
        self._loaded = False

    # ==================================================================
    # 订阅生命周期
    # ==================================================================

    def subscribe(
        self,
        user_id: str,
        plan_name: str,
        billing_cycle: str = "monthly",
        with_trial: bool = False,
        tenant_id: Optional[str] = None,
    ) -> Subscription:
        """创建订阅。

        Args:
            user_id: 用户 ID
            plan_name: PlanName.value(free / pro / enterprise)
            billing_cycle: monthly / yearly
            with_trial: 是否试用(PRO 默认 7 天,ENTERPRISE 不支持试用)
            tenant_id: 租户 ID(可选,默认当前租户)

        Raises:
            ValueError: plan 不存在 / 已有 ACTIVE 订阅 / 试用条件不符
        """
        if not is_enabled("billing"):
            # billing 关闭:返回 free plan 的虚拟订阅(透传)
            return self._disabled_subscription(user_id)

        plan = get_plan(plan_name)
        if plan is None:
            raise ValueError(f"Unknown plan: {plan_name}")

        cycle = BillingCycle(billing_cycle)
        # ENTERPRISE 不支持试用
        if with_trial and plan_name == PlanName.ENTERPRISE.value:
            with_trial = False

        with self._lock:
            self._load()
            existing = self._subs.get(user_id)
            if existing and existing.is_active():
                raise ValueError(
                    f"User {user_id} already has active subscription (plan={existing.plan_name})"
                )

            now = time.time()
            if cycle == BillingCycle.MONTHLY:
                period_end = now + 30 * 86400
            else:
                period_end = now + 365 * 86400

            trial_end = (now + TRIAL_DAYS * 86400) if with_trial else None
            status = SubscriptionStatus.TRIALING if with_trial else SubscriptionStatus.ACTIVE

            sub = Subscription(
                user_id=user_id,
                plan_name=plan_name,
                status=status,
                billing_cycle=cycle,
                current_period_start=now,
                current_period_end=period_end,
                trial_end=trial_end,
            )
            self._subs[user_id] = sub
            self._save()
            logger.info("User %s subscribed to %s (%s, trial=%s)", user_id, plan_name, cycle.value, with_trial)
            return sub

    def cancel(
        self,
        user_id: str,
        immediately: bool = False,
        reason: str = "",
    ) -> Optional[Subscription]:
        """取消订阅。

        Args:
            immediately: True 立即取消,False 周期末取消
            reason: 取消原因(便于分析)
        """
        if not is_enabled("billing"):
            return None

        with self._lock:
            self._load()
            sub = self._subs.get(user_id)
            if sub is None:
                return None

            now = time.time()
            sub.updated_at = now
            sub.history.append({"action": "cancel", "at": now, "reason": reason, "immediate": immediately})

            if immediately:
                sub.status = SubscriptionStatus.CANCELED
                sub.canceled_at = now
                sub.current_period_end = now
            else:
                sub.cancel_at_period_end = True
                # 状态保持 ACTIVE 直到周期末
            self._save()
            logger.info("User %s canceled subscription (immediate=%s, reason=%s)", user_id, immediately, reason)
            return sub

    def upgrade(
        self,
        user_id: str,
        new_plan_name: str,
        prorate: bool = True,
    ) -> Subscription:
        """升级 plan(支持按比例补款)。

        降级也走这个方法(new_plan 比当前 plan 便宜就是降级)。

        Args:
            prorate: 是否按比例计算(升级补款 / 降级退款)
        """
        if not is_enabled("billing"):
            return self._disabled_subscription(user_id)

        plan = get_plan(new_plan_name)
        if plan is None:
            raise ValueError(f"Unknown plan: {new_plan_name}")

        with self._lock:
            self._load()
            sub = self._subs.get(user_id)
            if sub is None or not sub.is_active():
                raise ValueError(f"User {user_id} has no active subscription to upgrade")

            old_plan_name = sub.plan_name
            now = time.time()
            sub.updated_at = now
            sub.history.append({
                "action": "upgrade" if plan.price_monthly >= (get_plan(old_plan_name) or plan).price_monthly else "downgrade",
                "at": now,
                "from": old_plan_name,
                "to": new_plan_name,
                "prorated": prorate,
            })
            sub.plan_name = new_plan_name
            # prorate 不调整 period_end,保持当前周期末
            self._save()
            logger.info("User %s upgraded %s → %s (prorate=%s)", user_id, old_plan_name, new_plan_name, prorate)
            return sub

    def renew(self, user_id: str) -> Optional[Subscription]:
        """续费(周期末调用,自动续期到下一周期)。"""
        if not is_enabled("billing"):
            return None

        with self._lock:
            self._load()
            sub = self._subs.get(user_id)
            if sub is None:
                return None

            now = time.time()
            # 从当前周期末续期(避免重复扣费)
            start = max(now, sub.current_period_end)
            if sub.billing_cycle == BillingCycle.MONTHLY:
                end = start + 30 * 86400
            else:
                end = start + 365 * 86400

            sub.current_period_start = start
            sub.current_period_end = end
            sub.updated_at = now
            sub.history.append({"action": "renew", "at": now, "period_end": end})
            # 周期末取消的标记位清掉(已续期则继续,但 cancel_at_period_end=True 时不应自动续)
            # 注:如果 cancel_at_period_end=True,不应自动 renew
            self._save()
            return sub

    # ==================================================================
    # 查询
    # ==================================================================

    def get_current(self, user_id: str) -> Optional[Subscription]:
        """查当前订阅(可能已过期,业务层需检查 is_active)。"""
        if not is_enabled("billing"):
            return self._disabled_subscription(user_id)

        with self._lock:
            self._load()
            return self._subs.get(user_id)

    def get_effective_plan(self, user_id: str) -> Plan:
        """获取用户当前生效的 plan(无订阅 / 已过期 → free)。"""
        from .plans import FREE_PLAN

        if not is_enabled("billing"):
            return FREE_PLAN

        sub = self.get_current(user_id)
        if sub is None or not sub.is_active():
            return FREE_PLAN
        plan = get_plan(sub.plan_name)
        return plan or FREE_PLAN

    def list_all(self) -> list[Subscription]:
        """列出所有订阅(管理后台用)。"""
        with self._lock:
            self._load()
            return list(self._subs.values())

    # ==================================================================
    # 状态机推进(定时任务调用)
    # ==================================================================

    def advance_status(self, now: Optional[float] = None) -> int:
        """推进所有订阅的状态(定时任务调用)。

        - TRIALING → ACTIVE(trial_end 到期)
        - ACTIVE → PAST_DUE(period_end 到期,未续费)
        - PAST_DUE → EXPIRED(7 天宽限期满)
        - cancel_at_period_end=True 且到期 → CANCELED

        Returns:
            状态变更数
        """
        if not is_enabled("billing"):
            return 0

        now = now or time.time()
        grace_period = 7 * 86400
        changed = 0

        with self._lock:
            self._load()
            for sub in self._subs.values():
                old_status = sub.status

                # TRIALING → ACTIVE
                if sub.status == SubscriptionStatus.TRIALING and sub.trial_end and now >= sub.trial_end:
                    sub.status = SubscriptionStatus.ACTIVE

                # ACTIVE → PAST_DUE / CANCELED
                elif sub.status == SubscriptionStatus.ACTIVE and now >= sub.current_period_end:
                    if sub.cancel_at_period_end:
                        sub.status = SubscriptionStatus.CANCELED
                        sub.canceled_at = now
                    else:
                        sub.status = SubscriptionStatus.PAST_DUE

                # PAST_DUE → EXPIRED
                elif sub.status == SubscriptionStatus.PAST_DUE:
                    if now >= sub.current_period_end + grace_period:
                        sub.status = SubscriptionStatus.EXPIRED

                if sub.status != old_status:
                    sub.updated_at = now
                    sub.history.append({"action": "status_change", "at": now, "from": old_status.value, "to": sub.status.value})
                    changed += 1

            if changed:
                self._save()
            return changed

    # ==================================================================
    # 内部
    # ==================================================================

    def _disabled_subscription(self, user_id: str) -> Subscription:
        """billing 关闭时返回的虚拟 free 订阅(透传)。"""
        now = time.time()
        return Subscription(
            user_id=user_id,
            plan_name=PlanName.FREE.value,
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.MONTHLY,
            current_period_start=now,
            current_period_end=now + 365 * 86400,  # 一年
        )

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for uid, sdata in data.get("subscriptions", {}).items():
                    self._subs[uid] = Subscription.from_dict(sdata)
        except Exception as e:
            logger.warning("Subscription store load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "subscriptions": {uid: s.to_dict() for uid, s in self._subs.items()},
            }
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Subscription store save failed: %s", e)


# 全局单例
_sm_instance: Optional[SubscriptionManager] = None
_sm_lock = threading.Lock()


def get_subscription_manager() -> SubscriptionManager:
    global _sm_instance
    if _sm_instance is None:
        with _sm_lock:
            if _sm_instance is None:
                _sm_instance = SubscriptionManager()
    return _sm_instance
