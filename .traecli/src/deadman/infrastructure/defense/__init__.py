"""防御性工程模块(D1-D10 跨层联动风险缓解)。

v1.4 深挖发现的跨层联动风险,单层测试无法发现,必须由独立的 defense 模块统一处理。

模块结构:
    - budget_coordinator.py(D1+D3):跨会话用户级 budget 隔离 + 全局 token budget 协调
    - tenant_circuit_breaker.py(D2):熔断器按租户隔离(防止单租户拖垮全平台)
    - pii_guard.py(D4):记忆压缩 PII 保留 + 检测(防止压缩后 PII 泄漏)
    - cache_protection.py(D5):缓存击穿 / 穿透 / 雪崩防护(singleflight + 空值防穿透)
    - degradation_guard.py(D6):降级风暴防护(防止多机制同时降级导致服务不可用)
    - cascading_guard.py(D7):级联故障防护(依赖链路故障检测 + 隔离)
    - chain_circuit_breaker.py(D8):降级链独立熔断(防链式降级失败,末级规则兜底)
    - trace_anonymizer.py(D9):跨 session trace 脱敏(防行为画像泄漏 + LDP 聚合)
    - master_key_backup.py(D10):主密钥 SSS 备份(防主密钥丢失致业务停摆)

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用,无副作用,关闭后透传)
"""

from __future__ import annotations

from .budget_coordinator import (
    BudgetAllocation,
    BudgetCoordinator,
    BudgetScope,
    get_budget_coordinator,
)
from .tenant_circuit_breaker import (
    TenantCircuitBreaker,
    TenantCircuitBreakerRegistry,
    get_tenant_cb,
)
from .pii_guard import (
    PIIMatch,
    PIIRedactor,
    PIIResult,
    PIIType,
    RedactStrategy,
    get_pii_redactor,
)
from .cache_protection import (
    CacheProtection,
    CacheStats,
    SingleflightResult,
    get_cache_protection,
)
from .degradation_guard import (
    DegradationEvent,
    DegradationGuard,
    DegradationLevel,
    get_degradation_guard,
)
from .cascading_guard import (
    CascadingGuard,
    DependencyNode,
    DependencyState,
    get_cascading_guard,
)
from .chain_circuit_breaker import (
    ChainCallResult,
    ChainCircuitBreaker,
    DegradationChain,
    FallbackReason,
    get_or_create_chain,
    list_chains,
    reset_all_chains,
)
from .trace_anonymizer import (
    BehaviorAggregator,
    BehaviorPattern,
    CrossSessionLinker,
    LinkConsent,
    TraceAnonymizer,
    TraceLinkStrategy,
    get_trace_anonymizer,
)
from .master_key_backup import (
    BackupStatus,
    DrillRecord,
    KeyShare,
    MasterKeyBackup,
    ShamirSecretSharing,
    get_master_key_backup,
)

__all__ = [
    # budget_coordinator (D1 + D3)
    "BudgetAllocation",
    "BudgetCoordinator",
    "BudgetScope",
    "get_budget_coordinator",
    # tenant_circuit_breaker (D2)
    "TenantCircuitBreaker",
    "TenantCircuitBreakerRegistry",
    "get_tenant_cb",
    # pii_guard (D4)
    "PIIMatch",
    "PIIRedactor",
    "PIIResult",
    "PIIType",
    "RedactStrategy",
    "get_pii_redactor",
    # cache_protection (D5)
    "CacheProtection",
    "CacheStats",
    "SingleflightResult",
    "get_cache_protection",
    # degradation_guard (D6)
    "DegradationEvent",
    "DegradationGuard",
    "DegradationLevel",
    "get_degradation_guard",
    # cascading_guard (D7)
    "CascadingGuard",
    "DependencyNode",
    "DependencyState",
    "get_cascading_guard",
    # chain_circuit_breaker (D8)
    "ChainCallResult",
    "ChainCircuitBreaker",
    "DegradationChain",
    "FallbackReason",
    "get_or_create_chain",
    "list_chains",
    "reset_all_chains",
    # trace_anonymizer (D9)
    "BehaviorAggregator",
    "BehaviorPattern",
    "CrossSessionLinker",
    "LinkConsent",
    "TraceAnonymizer",
    "TraceLinkStrategy",
    "get_trace_anonymizer",
    # master_key_backup (D10)
    "BackupStatus",
    "DrillRecord",
    "KeyShare",
    "MasterKeyBackup",
    "ShamirSecretSharing",
    "get_master_key_backup",
]
