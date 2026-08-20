"""P8.17-P8.27 AI 治理框架 - 模型卡 / 数据卡 / 风险卡 / 透明度报告 /
AI 红线 / 用户复议 / 伦理委员会 / 责任保险 / GovernanceManager 编排。

法规与规范依据:
    - 中国《生成式人工智能服务管理暂行办法》(2023)
    - 中国《互联网信息服务算法推荐管理规定》(2022)
    - GDPR Article 22 (自动化决策反对权)
    - Google Model Card Toolkit (Mitchell et al., 2019)
    - Datasheets for Datasets (Gebru et al., 2018)
    - NIST AI Risk Management Framework (AI RMF 1.0, 2023)
    - Anthropic Responsible Scaling Policy
    - EU AI Act Annex III (high-risk AI 须人工监督)

模块结构:
    - model_card.py        # 模型卡 (Google Model Card 风格)
    - data_card.py         # 数据卡 (Datasheets for Datasets 风格)
    - risk_card.py         # 风险卡 + 矩阵评分
    - transparency.py      # AI 透明度报告 (周期性)
    - ai_redlines.py       # AI 红线硬编码 enforcement (CODE-LEVEL)
    - appeals.py           # 用户复议机制 (SLA 7d)
    - ethics_committee.py  # 伦理委员会审查 (quorum + 数字孪生逝者)
    - liability_insurance.py  # AI 责任保险 (record-keeping)
    - manager.py           # 高层 GovernanceManager 编排

设计:
    - 所有持久化按租户隔离 (resolve_data_path)
    - 原子写 (.tmp + os.replace) + 线程安全 (RLock)
    - feature flag DEADMAN_GOVERNANCE_ENABLED=0 (默认 OFF)
      → 资产注册 / 复议 / 报告 / 伦理 等业务接口 raise GovernanceDisabledError
      → AI redlines 仍 enforce (底线保护,见 ai_redlines.py 注释)
    - 单例模式 _xxx_instance + _xxx_lock

feature flag:`DEADMAN_GOVERNANCE_ENABLED=0` 默认关闭。
"""

from __future__ import annotations

from .ai_redlines import (
    AIRedline,
    RedlineCategory,
    RedlineResult,
    RedlineRule,
    RedlineViolation,
    get_ai_redline,
    reset_ai_redline,
)
from .appeals import (
    APPEAL_SLA_SECONDS,
    Appeal,
    AppealDecision,
    AppealsManager,
    AppealStatus,
    get_appeals_manager,
)
from .data_card import (
    DataCard,
    DataCardRegistry,
    SensitivityLevel,
    get_data_card_registry,
)
from .ethics_committee import (
    DIGITAL_TWIN_DECEASED_CATEGORY,
    CaseDecision,
    CaseStatus,
    CommitteeMember,
    EthicsCase,
    EthicsCommittee,
    MemberRole,
    get_ethics_committee,
)
from .liability_insurance import (
    ClaimStatus,
    CoverageType,
    InsuranceClaim,
    InsurancePolicy,
    LiabilityInsurance,
    get_liability_insurance,
)
from .manager import (
    GovernanceDecision,
    GovernanceDisabledError,
    GovernanceManager,
    get_governance_manager,
    reset_governance_manager,
)
from .model_card import ModelCard, ModelCardRegistry, get_model_card_registry
from .risk_card import (
    ETHICS_COMMITTEE_THRESHOLD,
    REVIEW_THRESHOLD,
    RiskAssessment,
    RiskCard,
    RiskCategory,
    RiskLikelihood,
    RiskScore,
    RiskSeverity,
    RiskStatus,
    get_risk_assessment,
)
from .transparency import (
    ReportPeriod,
    TransparencyReport,
    TransparencyReporter,
    get_transparency_reporter,
)

__all__ = [
    # model_card
    "ModelCard",
    "ModelCardRegistry",
    "get_model_card_registry",
    # data_card
    "DataCard",
    "DataCardRegistry",
    "SensitivityLevel",
    "get_data_card_registry",
    # risk_card
    "RiskCard",
    "RiskScore",
    "RiskAssessment",
    "RiskCategory",
    "RiskSeverity",
    "RiskLikelihood",
    "RiskStatus",
    "REVIEW_THRESHOLD",
    "ETHICS_COMMITTEE_THRESHOLD",
    "get_risk_assessment",
    # transparency
    "TransparencyReport",
    "TransparencyReporter",
    "ReportPeriod",
    "get_transparency_reporter",
    # ai_redlines
    "AIRedline",
    "RedlineCategory",
    "RedlineRule",
    "RedlineResult",
    "RedlineViolation",
    "get_ai_redline",
    "reset_ai_redline",
    # appeals
    "Appeal",
    "AppealStatus",
    "AppealDecision",
    "AppealsManager",
    "APPEAL_SLA_SECONDS",
    "get_appeals_manager",
    # ethics_committee
    "CommitteeMember",
    "MemberRole",
    "EthicsCase",
    "CaseStatus",
    "CaseDecision",
    "EthicsCommittee",
    "DIGITAL_TWIN_DECEASED_CATEGORY",
    "get_ethics_committee",
    # liability_insurance
    "InsurancePolicy",
    "InsuranceClaim",
    "ClaimStatus",
    "CoverageType",
    "LiabilityInsurance",
    "get_liability_insurance",
    # manager
    "GovernanceManager",
    "GovernanceDecision",
    "GovernanceDisabledError",
    "get_governance_manager",
    "reset_governance_manager",
]

# 模块级单例引用 (与 compliance 模块保持一致,便于测试 reset)
# 注意:_gm_instance 等定义在各自模块,这里通过 reset 函数清空。
