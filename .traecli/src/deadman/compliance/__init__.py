"""P8.6 合规与监管 - 数据驻留 + 数据可删除权 + AI 内容标识 + 审计上报。

法规依据:
    - 中国《个人信息保护法》(PIPL,2021)
    - 中国《数据安全法》(DSL,2021)
    - 中国《生成式人工智能服务管理暂行办法》(2023)
    - GDPR(若服务欧盟用户)
    - 等保 2.0 / 3.0(若部署在国内)

模块结构:
    - data_residency.py: 数据驻留(不出境)
    - right_to_delete.py: 数据可删除权(7 天彻底清除)
    - ai_labeling.py: AI 内容标识(水印 + 文本声明)
    - audit_report.py: 监管上报接口
    - retention.py: 数据保留策略(7 年)
    - consent.py: 用户同意管理(明示同意 + 撤回)

feature flag:`DEADMAN_COMPLIANCE_ENABLED=0` 默认关闭。
"""

from __future__ import annotations

from .data_residency import DataRegion, DataResidency, ResidencyViolation, get_data_residency
from .right_to_delete import (
    DeletionRequest,
    DeletionStatus,
    RightToDelete,
    get_right_to_delete,
)
from .ai_labeling import AILabeling, LabelType, get_ai_labeling
from .audit_report import AuditReport, AuditReporter, ReportFrequency, get_audit_reporter
from .retention import DataCategory, RetentionPolicy, RetentionManager, get_retention_manager
from .consent import ConsentManager, ConsentRecord, ConsentStatus, ConsentType, get_consent_manager

__all__ = [
    # data_residency
    "DataRegion",
    "DataResidency",
    "ResidencyViolation",
    "get_data_residency",
    # right_to_delete
    "DeletionRequest",
    "DeletionStatus",
    "RightToDelete",
    "get_right_to_delete",
    # ai_labeling
    "AILabeling",
    "LabelType",
    "get_ai_labeling",
    # audit_report
    "AuditReport",
    "AuditReporter",
    "ReportFrequency",
    "get_audit_reporter",
    # retention
    "DataCategory",
    "RetentionPolicy",
    "RetentionManager",
    "get_retention_manager",
    # consent
    "ConsentManager",
    "ConsentRecord",
    "ConsentStatus",
    "ConsentType",
    "get_consent_manager",
]
