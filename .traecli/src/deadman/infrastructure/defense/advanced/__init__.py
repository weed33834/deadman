"""D11-D34 高级防御性工程模块(v1.5 + v1.6 + v1.7 终极深挖产物)。

本子包包含 v1.5 终极深挖识别的 D11-D20 防御性工程,
v1.6 补充的 D21/D25 关键防御性工程,
以及 v1.7 补充的 D31/D33/D34 防御性工程,
作为长期演进的"压舱石",在 v1.4 D1-D10 基础上补齐。

模块结构:
    - llm_capability_tier.py(D11):LLM 能力分级抽象
    - multimodal_guardrail.py(D12):多模态流水线护栏
    - vector_store_tenant_isolation.py(D13):向量库租户隔离
    - marketplace_sandbox_hardener.py(D14):Marketplace 沙箱增强
    - regulatory_change_notifier.py(D15):法规变更通知机制
    - provider_style_normalizer.py(D16):多 provider 风格归一化
    - reflexion_sanitizer.py(D17):Reflexion 策略脱敏
    - task_complexity_router.py(D18):任务复杂度路由
    - edge_inference_security.py(D19):边缘推理硬件安全
    - regional_compliance.py(D20):区域化合规模块
    - inference_compute_governor.py(D21):推理时计算治理(o1/o3/R1 思考预算)
    - convergence_detector.py(D25):多智能体收敛检测(防回声室/共谋)
    - memory_integrity_verifier.py(D31):记忆完整性验证(防投毒 + hash chain)
    - constitutional_drift_detector.py(D33):宪法漂移检测(防护栏阈值慢漂移)
    - cross_model_collusion_detector.py(D34):跨模型共谋检测(防多 provider 共谋)

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用,关闭后透传)。
"""

from __future__ import annotations

# D11: LLM 能力分级抽象
from .llm_capability_tier import (
    CapabilityRequirement,
    CapabilityRouter,
    CapabilityTier,
    ModelCapability,
    ModelProfile,
    get_capability_router,
    reset_capability_router,
)

# D12: 多模态流水线护栏
from .multimodal_guardrail import (
    GuardrailAction,
    GuardrailDecision,
    MultimodalGuardrail,
    get_multimodal_guardrail,
    reset_multimodal_guardrail,
)

# D13: 向量库租户隔离
from .vector_store_tenant_isolation import (
    IsolationMode,
    TenantIsolationError,
    TenantVectorStats,
    TenantVectorStore,
    get_global_tenant_vector_store,
    reset_global_tenant_vector_store,
    set_global_tenant_vector_store,
)

# D14: Marketplace 沙箱增强
from .marketplace_sandbox_hardener import (
    FilesystemGuard,
    SandboxHardener,
    StaticCheckResult,
    StaticCheckViolation,
    get_sandbox_hardener,
    reset_sandbox_hardener,
)

# D15: 法规变更通知机制
from .regulatory_change_notifier import (
    ChangeSeverity,
    NotificationChannel,
    RegulatoryChange,
    RegulatoryChangeDetector,
    Subscriber,
    get_regulatory_change_detector,
    reset_regulatory_change_detector,
    severity_at_least,
)

# D16: 多 provider 风格归一化
from .provider_style_normalizer import (
    Provider,
    ProviderStyleAdapter,
    StyleDriftReport,
    StyleNormalizer,
    StyleProfile,
    ToneStyle,
    get_style_normalizer,
    reset_style_normalizer,
)

# D17: Reflexion 策略脱敏
from .reflexion_sanitizer import (
    ReflexionSanitizer,
    SanitizationResult,
    get_reflexion_sanitizer,
    hash_user_id,
    reset_reflexion_sanitizer,
)

# D18: 任务复杂度路由
from .task_complexity_router import (
    ComplexityClassifier,
    ComplexityRouter,
    ComplexitySignals,
    RoutingDecision,
    RoutingStrategy,
    TaskComplexity,
    get_complexity_classifier,
    get_complexity_router,
    reset_complexity_router,
)

# D19: 边缘推理硬件安全
from .edge_inference_security import (
    InferenceAuditRecord,
    InferenceAuditor,
    ModelSignature,
    ModelSignatureVerifier,
    TEEAbstraction,
    VerificationResult,
    VerificationStatus,
    get_inference_auditor,
    get_model_signature_verifier,
    get_tee_abstraction,
    reset_edge_security_singletons,
)

# D20: 区域化合规模块
from .regional_compliance import (
    ComplianceCheckResult,
    ComplianceLevel,
    ComplianceViolation,
    DataKind,
    RegionalComplianceOrchestrator,
    UnifiedRegion,
    data_region_to_unified,
    get_regional_compliance_orchestrator,
    jurisdiction_to_unified,
    reset_regional_compliance_orchestrator,
    unified_to_data_region,
    unified_to_jurisdiction,
)

# D21: 推理时计算治理(v1.6)
from .inference_compute_governor import (
    ComputeGovernor,
    DegradeReason,
    InferenceBudgetPlan,
    ReasoningAuditResult,
    ReasoningAuditor,
    ReasoningModelStyle,
    UserComputeStats,
    get_compute_governor,
    reset_compute_governor,
)

# D25: 多智能体收敛检测(v1.6)
from .convergence_detector import (
    AgentOutput,
    AlertSeverity,
    AntiPattern,
    ConvergenceAlert,
    ConvergenceCheckResult,
    ConvergenceDetector,
    ConvergenceMetrics,
    CountermeasureStrategy,
    get_convergence_detector,
    reset_convergence_detector,
)

# D31: 记忆完整性验证(v1.7)
from .memory_integrity_verifier import (
    ChainVerificationResult,
    IntegrityViolation,
    MemoryIntegrityVerifier,
    MemoryRecord,
    MemorySource,
    TrustLevel,
    ViolationType,
    get_memory_integrity_verifier,
    reset_memory_integrity_verifier,
)

# D33: 宪法漂移检测(v1.7)
from .constitutional_drift_detector import (
    ChangeReason,
    ConstitutionalDriftDetector,
    DriftAlert,
    DriftDirection,
    DriftReport,
    DriftSeverity,
    ThresholdSnapshot,
    ThresholdType,
    get_constitutional_drift_detector,
    reset_constitutional_drift_detector,
)

# D34: 跨模型共谋检测(v1.7)
from .cross_model_collusion_detector import (
    CollusionAlert,
    CollusionCheckResult,
    CollusionMetrics,
    CollusionPattern,
    Countermeasure,
    CrossModelCollusionDetector,
    ModelProvider,
    ProviderOutput,
    get_cross_model_collusion_detector,
    reset_cross_model_collusion_detector,
)


__all__ = [
    # D11: LLM 能力分级抽象
    "CapabilityRequirement",
    "CapabilityRouter",
    "CapabilityTier",
    "ModelCapability",
    "ModelProfile",
    "get_capability_router",
    "reset_capability_router",
    # D12: 多模态流水线护栏
    "GuardrailAction",
    "GuardrailDecision",
    "MultimodalGuardrail",
    "get_multimodal_guardrail",
    "reset_multimodal_guardrail",
    # D13: 向量库租户隔离
    "IsolationMode",
    "TenantIsolationError",
    "TenantVectorStats",
    "TenantVectorStore",
    "get_global_tenant_vector_store",
    "reset_global_tenant_vector_store",
    "set_global_tenant_vector_store",
    # D14: Marketplace 沙箱增强
    "FilesystemGuard",
    "SandboxHardener",
    "StaticCheckResult",
    "StaticCheckViolation",
    "get_sandbox_hardener",
    "reset_sandbox_hardener",
    # D15: 法规变更通知机制
    "ChangeSeverity",
    "NotificationChannel",
    "RegulatoryChange",
    "RegulatoryChangeDetector",
    "Subscriber",
    "get_regulatory_change_detector",
    "reset_regulatory_change_detector",
    "severity_at_least",
    # D16: 多 provider 风格归一化
    "Provider",
    "ProviderStyleAdapter",
    "StyleDriftReport",
    "StyleNormalizer",
    "StyleProfile",
    "ToneStyle",
    "get_style_normalizer",
    "reset_style_normalizer",
    # D17: Reflexion 策略脱敏
    "ReflexionSanitizer",
    "SanitizationResult",
    "get_reflexion_sanitizer",
    "hash_user_id",
    "reset_reflexion_sanitizer",
    # D18: 任务复杂度路由
    "ComplexityClassifier",
    "ComplexityRouter",
    "ComplexitySignals",
    "RoutingDecision",
    "RoutingStrategy",
    "TaskComplexity",
    "get_complexity_classifier",
    "get_complexity_router",
    "reset_complexity_router",
    # D19: 边缘推理硬件安全
    "InferenceAuditRecord",
    "InferenceAuditor",
    "ModelSignature",
    "ModelSignatureVerifier",
    "TEEAbstraction",
    "VerificationResult",
    "VerificationStatus",
    "get_inference_auditor",
    "get_model_signature_verifier",
    "get_tee_abstraction",
    "reset_edge_security_singletons",
    # D20: 区域化合规模块
    "ComplianceCheckResult",
    "ComplianceLevel",
    "ComplianceViolation",
    "DataKind",
    "RegionalComplianceOrchestrator",
    "UnifiedRegion",
    "data_region_to_unified",
    "get_regional_compliance_orchestrator",
    "jurisdiction_to_unified",
    "reset_regional_compliance_orchestrator",
    "unified_to_data_region",
    "unified_to_jurisdiction",
    # D21: 推理时计算治理(v1.6)
    "ComputeGovernor",
    "DegradeReason",
    "InferenceBudgetPlan",
    "ReasoningAuditResult",
    "ReasoningAuditor",
    "ReasoningModelStyle",
    "UserComputeStats",
    "get_compute_governor",
    "reset_compute_governor",
    # D25: 多智能体收敛检测(v1.6)
    "AgentOutput",
    "AlertSeverity",
    "AntiPattern",
    "ConvergenceAlert",
    "ConvergenceCheckResult",
    "ConvergenceDetector",
    "ConvergenceMetrics",
    "CountermeasureStrategy",
    "get_convergence_detector",
    "reset_convergence_detector",
    # D31: 记忆完整性验证(v1.7)
    "ChainVerificationResult",
    "IntegrityViolation",
    "MemoryIntegrityVerifier",
    "MemoryRecord",
    "MemorySource",
    "TrustLevel",
    "ViolationType",
    "get_memory_integrity_verifier",
    "reset_memory_integrity_verifier",
    # D33: 宪法漂移检测(v1.7)
    "ChangeReason",
    "ConstitutionalDriftDetector",
    "DriftAlert",
    "DriftDirection",
    "DriftReport",
    "DriftSeverity",
    "ThresholdSnapshot",
    "ThresholdType",
    "get_constitutional_drift_detector",
    "reset_constitutional_drift_detector",
    # D34: 跨模型共谋检测(v1.7)
    "CollusionAlert",
    "CollusionCheckResult",
    "CollusionMetrics",
    "CollusionPattern",
    "Countermeasure",
    "CrossModelCollusionDetector",
    "ModelProvider",
    "ProviderOutput",
    "get_cross_model_collusion_detector",
    "reset_cross_model_collusion_detector",
]
