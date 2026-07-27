"""P8.4 Agent Marketplace - 第三方 agent 注册中心 + 自动审核 + 评分 + Revenue Share + 沙盒执行。

设计原则(与 P7 infrastructure / P8 billing 对齐):
    - feature flag 默认关闭:`DEADMAN_MARKETPLACE_ENABLED=0`
      关闭时所有公共 API 抛 `MarketplaceError`,避免误用。
    - 静默降级 + 显式错误:审核 / 沙盒失败明示,持久化失败降级到日志。
    - 多租户隔离:与 P7.3 multi_tenant 协同,registry / ratings / revenue 按 tenant_id 分目录。
    - 持久化:与 P7.6 durable_execution 同款 `.tmp + os.replace` 原子写。
    - 线程安全:单实例读写各自加 `threading.RLock`,跨实例并发写靠原子 `os.replace` 保护。

模块结构:
    - registry.py: AgentListing + MarketplaceRegistry(注册 / 审核 / 浏览 / 搜索 / 升级)
    - reviewer.py: ReviewResult + AgentReviewer(安全扫描 + schema 校验 + PII 检测 + 评分)
    - rating.py: Rating + RatingSystem(用户评分 + helpful vote + flag)
    - revenue.py: RevenueSplit + RevenueShare(用量记录 + 分账 + payout,纯记账无真实资金流)
    - sandbox.py: SandboxConfig + MarketplaceSandbox(资源限制 + 工具白名单 + PII 脱敏)

约束:
    - 所有 payment / revenue / payout 仅记录,不涉及真实资金流。
    - 所有依赖可选,沙盒仅用 Python 内置 `resource` / `signal`,不引入第三方沙盒库。
    - PII 双向脱敏:沙盒入口对 input 脱敏,出口对 output 脱敏(借 `defense.pii_guard.PIIRedactor`)。
    - 成本可控:沙盒执行前向 `defense.budget_coordinator.BudgetCoordinator` 申请预算并释放。

feature flag:`DEADMAN_MARKETPLACE_ENABLED=0`(默认关闭,关闭时所有 API 抛 MarketplaceError)
"""

from __future__ import annotations

from .registry import (
    AgentListing,
    ListingCategory,
    ListingSort,
    ListingStatus,
    MarketplaceError,
    MarketplaceRegistry,
    get_marketplace_registry,
)
from .reviewer import (
    AgentReviewer,
    ReviewIssue,
    ReviewResult,
    get_agent_reviewer,
)
from .rating import (
    Rating,
    RatingFlag,
    RatingSystem,
    get_rating_system,
)
from .revenue import (
    PayoutRecord,
    RevenueShare,
    RevenueSplit,
    UsageRecord,
    get_revenue_share,
)
from .sandbox import (
    SandboxConfig,
    SandboxResult,
    MarketplaceSandbox,
    get_marketplace_sandbox,
)
from .skill_manager import (
    SkillError,
    SkillManager,
    get_skill_manager,
)


def get_marketplace() -> MarketplaceRegistry:
    """获取 marketplace 默认入口(MarketplaceRegistry 单例)。

    与 billing.get_subscription_manager 同款便捷入口;
    其他子模块(reviewer / rating / revenue / sandbox)通过各自的 get_xxx 单例访问。
    """
    return get_marketplace_registry()


__all__ = [
    # registry
    "AgentListing",
    "ListingCategory",
    "ListingSort",
    "ListingStatus",
    "MarketplaceError",
    "MarketplaceRegistry",
    "get_marketplace_registry",
    "get_marketplace",
    # reviewer
    "AgentReviewer",
    "ReviewIssue",
    "ReviewResult",
    "get_agent_reviewer",
    # rating
    "Rating",
    "RatingFlag",
    "RatingSystem",
    "get_rating_system",
    # revenue
    "PayoutRecord",
    "RevenueShare",
    "RevenueSplit",
    "UsageRecord",
    "get_revenue_share",
    # sandbox
    "SandboxConfig",
    "SandboxResult",
    "MarketplaceSandbox",
    "get_marketplace_sandbox",
    # skill_manager
    "SkillError",
    "SkillManager",
    "get_skill_manager",
]
