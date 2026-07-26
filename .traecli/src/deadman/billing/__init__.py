"""P8.1 商业化与计费 - 多 plan 订阅 + 用量计量 + 账单/发票 + 成本路由。

设计原则(与 P7 infrastructure 对齐):
    - feature flag 默认关闭:`DEADMAN_BILLING_ENABLED=0`
      关闭时所有调用透传,quota 链路不受影响(降级到 P7.7 quota 默认值)。
    - 静默降级:任何 billing 异常都不抛到业务层,只记日志。
    - 多租户隔离:与 P7.3 multi_tenant 协同,subscription / usage 按 tenant_id 索引。
    - 持久化:与 P7.6 durable_execution 同款 .tmp + os.replace 原子写。

模块结构:
    - plans.py: Plan 定义(free / pro / enterprise)+ PlanLimits
    - subscription.py: SubscriptionManager 订阅管理(增删改查 + 续费 + 退订 + 升降级)
    - metering.py: MeteringService 多维度用量计量(token / 工具调用 / 存储 / 多模态)
    - usage_tracker.py: UsageTracker 实时计数 + 持久化 + 配额检查(与 P7.7 quota 协同)
    - invoice.py: InvoiceGenerator 账单生成 + 发票导出(PDF / CSV)+ 多支付网关(Stripe / 支付宝 / 微信)
    - cost_router.py: CostRouter 多模型成本最优路由(SLA + 成本权衡)

集成策略:
    - 与 P7.7 quota 协同:billing 是上层(决定 plan),quota 是下层(执行 plan 的限制)
    - 与 P8.6 compliance 协同:发票数据按租户隔离,跨境支付需用户同意
    - 与 P8.7 alignment 协同:cost_router 决定用便宜模型还是强模型
"""

from __future__ import annotations

from .plans import Plan, PlanLimits, PlanName, PLANS, get_plan
from .subscription import (
    Subscription,
    SubscriptionManager,
    SubscriptionStatus,
    get_subscription_manager,
)
from .metering import (
    MeteringEvent,
    MeteringService,
    MeteringDimension,
    get_metering_service,
)
from .usage_tracker import UsageReport, UsageTracker, get_usage_tracker
from .invoice import (
    Invoice,
    InvoiceGenerator,
    InvoiceLineItem,
    InvoiceStatus,
    PaymentGateway,
    get_invoice_generator,
)
from .cost_router import (
    CostRouter,
    ModelChoice,
    ModelTier,
    RoutingResult,
    RoutingStrategy,
    get_cost_router,
)

__all__ = [
    # plans
    "Plan",
    "PlanLimits",
    "PlanName",
    "PLANS",
    "get_plan",
    # subscription
    "Subscription",
    "SubscriptionManager",
    "SubscriptionStatus",
    "get_subscription_manager",
    # metering
    "MeteringEvent",
    "MeteringService",
    "MeteringDimension",
    "get_metering_service",
    # usage_tracker
    "UsageReport",
    "UsageTracker",
    "get_usage_tracker",
    # invoice
    "Invoice",
    "InvoiceGenerator",
    "InvoiceLineItem",
    "InvoiceStatus",
    "PaymentGateway",
    "get_invoice_generator",
    # cost_router
    "CostRouter",
    "ModelChoice",
    "ModelTier",
    "RoutingResult",
    "RoutingStrategy",
    "get_cost_router",
]
