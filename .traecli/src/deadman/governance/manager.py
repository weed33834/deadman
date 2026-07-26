"""P8.17 AI 治理框架 - 高层 GovernanceManager 编排所有子模块。

GovernanceManager 是治理框架的入口,编排 model_card / data_card / risk_card /
transparency / ai_redlines / appeals / ethics_committee / liability_insurance。

设计原则:
    - 单例模式 (get_governance_manager)
    - feature flag DEADMAN_GOVERNANCE_ENABLED=0 时所有操作 raise GovernanceDisabledError
      (与 compliance 静默降级不同,governance 关闭时显式报错,避免误用)
    - AI redlines 仍 enforce (底线保护,见 ai_redlines.py 注释)
    - before_action 在 AI 决策前调用,先做 redline 检查再做保险预检
    - after_decision 在 AI 决策后调用,记录用于透明度报告
    - 所有操作线程安全

集成点:
    - 业务路径在调用 AI 决策前必须调 before_action
    - 业务路径在 AI 决策返回后必须调 after_decision
    - 用户对 AI 决策不满时调 file_appeal
    - 周期性调 generate_transparency_report
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id
from .ai_redlines import AIRedline, RedlineResult, RedlineViolation, get_ai_redline
from .appeals import Appeal, AppealsManager, AppealDecision, get_appeals_manager
from .data_card import DataCard, DataCardRegistry, get_data_card_registry
from .ethics_committee import EthicsCase, EthicsCommittee, get_ethics_committee
from .liability_insurance import LiabilityInsurance, get_liability_insurance
from .model_card import ModelCard, ModelCardRegistry, get_model_card_registry
from .risk_card import RiskAssessment, RiskCard, get_risk_assessment
from .transparency import (
    ReportPeriod,
    TransparencyReport,
    TransparencyReporter,
    get_transparency_reporter,
)

logger = logging.getLogger(__name__)


class GovernanceDisabledError(RuntimeError):
    """治理模块被 feature flag 关闭时抛出。

    业务路径遇到此异常应:
      - 要么降级 (跳过 governance,但 AI redlines 仍 enforce)
      - 要么拒绝服务 (高敏感场景)
    """


@dataclass
class GovernanceDecision:
    """before_action 的返回结果。

    Attributes:
        allowed: 是否允许执行 action
        reason: 中文说明 (允许 / 禁止原因)
        redline_result: 红线检查结果
        insurance_covered: 是否在保险覆盖内
        checked_at: 检查时间戳
        action: 被检查的 action 描述
        context: 上下文 (dict,脱敏后)
        metadata: 附加元数据
    """

    allowed: bool
    reason: str
    redline_result: Optional[RedlineResult] = None
    insurance_covered: bool = False
    checked_at: float = field(default_factory=time.time)
    action: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class GovernanceManager:
    """高层治理编排器 - 单例。

    用法:
        gm = get_governance_manager()
        # 注册资产
        gm.register_model(ModelCard(...))
        gm.register_dataset(DataCard(...))
        gm.register_risk(RiskCard(...))

        # 决策前检查
        decision = gm.before_action("代签遗嘱", context={"role": "user"})
        if not decision.allowed:
            raise RedlineViolation(...)

        # 决策后记录
        gm.after_decision("decision-123", "user-1", "AI generated memorial letter")

        # 用户复议
        gm.file_appeal("user-1", "decision-123", "内容不符合预期")

        # 周期报告
        gm.generate_transparency_report(period_start, period_end)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 子模块单例 (lazy 引用)
        self._model_cards: ModelCardRegistry = get_model_card_registry()
        self._data_cards: DataCardRegistry = get_data_card_registry()
        self._risk_assessment: RiskAssessment = get_risk_assessment()
        self._transparency: TransparencyReporter = get_transparency_reporter()
        self._redline: AIRedline = get_ai_redline()
        self._appeals: AppealsManager = get_appeals_manager()
        self._ethics: EthicsCommittee = get_ethics_committee()
        self._insurance: LiabilityInsurance = get_liability_insurance()
        # 内部决策计数器 (用于透明度报告)
        self._decision_count = 0
        self._ai_decision_count = 0
        self._human_review_count = 0
        self._bias_incidents = 0
        self._model_usage: dict[str, int] = {}
        self._user_feedback: dict[str, int] = {}

    # ==================================================================
    # feature flag 检查
    # ==================================================================

    def _ensure_enabled(self) -> None:
        """治理模块是否启用 (关闭时抛 GovernanceDisabledError)。

        注意:AI redlines 即使 governance 关闭也仍 enforce (底线保护),
        所以 before_action 不走此检查。
        """
        if not is_enabled("governance"):
            raise GovernanceDisabledError(
                "AI governance framework is disabled "
                "(set DEADMAN_GOVERNANCE_ENABLED=1 to enable)"
            )

    # ==================================================================
    # 资产注册
    # ==================================================================

    def register_model(self, card: ModelCard) -> ModelCard:
        self._ensure_enabled()
        return self._model_cards.register(card)

    def register_dataset(self, card: DataCard) -> DataCard:
        self._ensure_enabled()
        return self._data_cards.register(card)

    def register_risk(self, card: RiskCard) -> RiskCard:
        self._ensure_enabled()
        return self._risk_assessment.register(card)

    # ==================================================================
    # 决策前 / 决策后
    # ==================================================================

    def before_action(
        self,
        action: str,
        context: Optional[dict[str, Any]] = None,
        incident_type: Optional[str] = None,
        amount: float = 0.0,
    ) -> GovernanceDecision:
        """决策前检查 - 红线 enforcement + 保险覆盖预检。

        Critical:红线检查不走 _ensure_enabled (底线保护),
        即使 governance 关闭也仍 enforce。

        Args:
            action: 动作描述 (中文 / 英文)
            context: 上下文 (含 role 等)
            incident_type: 保险覆盖类型 (CoverageType.value)
            amount: 涉及金额 (用于保险预检)

        Returns:
            GovernanceDecision (allowed=False 时不应执行 action)
        """
        ctx = context or {}

        # 1. 红线检查 (始终 enforce,即使 governance 关闭)
        redline_result = self._redline.is_allowed(action, ctx)
        if not redline_result.allowed:
            return GovernanceDecision(
                allowed=False,
                reason=f"红线禁止: {redline_result.reason}",
                redline_result=redline_result,
                insurance_covered=False,
                action=action,
                context=self._sanitize_context(ctx),
            )

        # 2. governance 关闭时,只过 redline 就放行 (不查保险 / 不记录)
        if not is_enabled("governance"):
            return GovernanceDecision(
                allowed=True,
                reason="governance disabled, redline passed",
                redline_result=redline_result,
                insurance_covered=False,
                action=action,
                context=self._sanitize_context(ctx),
            )

        # 3. 保险覆盖预检 (可选,关键动作前用)
        insurance_covered = False
        if incident_type:
            insurance_covered = self._insurance.check_coverage(incident_type, amount)

        return GovernanceDecision(
            allowed=True,
            reason="redline passed",
            redline_result=redline_result,
            insurance_covered=insurance_covered,
            action=action,
            context=self._sanitize_context(ctx),
            metadata={
                "incident_type": incident_type,
                "amount": amount,
            },
        )

    def after_decision(
        self,
        decision_id: str,
        user_id: str,
        decision_content: str,
        model_id: Optional[str] = None,
        is_ai: bool = True,
    ) -> None:
        """决策后记录 (用于透明度报告)。"""
        if not is_enabled("governance"):
            return
        with self._lock:
            self._decision_count += 1
            if is_ai:
                self._ai_decision_count += 1
            else:
                self._human_review_count += 1
            if model_id:
                self._model_usage[model_id] = self._model_usage.get(model_id, 0) + 1
            logger.debug(
                "Decision recorded: %s (user=%s ai=%s model=%s)",
                decision_id, user_id, is_ai, model_id,
            )

    def record_bias_incident(self, description: str = "") -> None:
        """记录偏见事件 (用于透明度报告)。"""
        if not is_enabled("governance"):
            return
        with self._lock:
            self._bias_incidents += 1
            logger.warning("Bias incident recorded: %s", description[:80])

    def record_user_feedback(self, category: str) -> None:
        """记录用户反馈分类。"""
        if not is_enabled("governance"):
            return
        with self._lock:
            self._user_feedback[category] = self._user_feedback.get(category, 0) + 1

    # ==================================================================
    # 复议
    # ==================================================================

    def file_appeal(
        self,
        user_id: str,
        decision_id: str,
        reason: str,
    ) -> Appeal:
        self._ensure_enabled()
        return self._appeals.file(user_id, decision_id, reason)

    # ==================================================================
    # 透明度报告
    # ==================================================================

    def generate_transparency_report(
        self,
        period_start: float,
        period_end: float,
        period: ReportPeriod = ReportPeriod.MONTHLY,
    ) -> TransparencyReport:
        self._ensure_enabled()
        report = self._transparency.generate_report(period_start, period_end, period)
        # 注入本 manager 收集的统计 (覆盖 audit_report 的部分)
        with self._lock:
            report.total_decisions = max(report.total_decisions, self._decision_count)
            report.ai_decisions_count = max(report.ai_decisions_count, self._ai_decision_count)
            report.human_review_count = max(report.human_review_count, self._human_review_count)
            report.bias_incidents_count = max(report.bias_incidents_count, self._bias_incidents)
            for mid, count in self._model_usage.items():
                report.model_usage_stats[mid] = max(
                    report.model_usage_stats.get(mid, 0), count
                )
            for cat, count in self._user_feedback.items():
                report.user_feedback_summary[cat] = max(
                    report.user_feedback_summary.get(cat, 0), count
                )
        # 持久化更新后的报告
        self._transparency.add_section(
            report.report_id, "manager_stats", "injected by GovernanceManager"
        )
        return report

    # ==================================================================
    # 伦理委员会
    # ==================================================================

    def submit_ethics_case(
        self,
        title: str,
        description: str,
        category: str,
        severity: str = "medium",
    ) -> EthicsCase:
        self._ensure_enabled()
        return self._ethics.submit_case(title, description, category, severity)

    # ==================================================================
    # 子模块访问器
    # ==================================================================

    @property
    def model_cards(self) -> ModelCardRegistry:
        return self._model_cards

    @property
    def data_cards(self) -> DataCardRegistry:
        return self._data_cards

    @property
    def risk_assessment(self) -> RiskAssessment:
        return self._risk_assessment

    @property
    def transparency(self) -> TransparencyReporter:
        return self._transparency

    @property
    def redline(self) -> AIRedline:
        return self._redline

    @property
    def appeals(self) -> AppealsManager:
        return self._appeals

    @property
    def ethics(self) -> EthicsCommittee:
        return self._ethics

    @property
    def insurance(self) -> LiabilityInsurance:
        return self._insurance

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _sanitize_context(ctx: dict[str, Any]) -> dict[str, Any]:
        """脱敏上下文 (避免泄漏 PII 到日志)。"""
        sanitized = {}
        sensitive_keys = {"password", "token", "secret", "api_key", "credential"}
        for k, v in ctx.items():
            if any(s in k.lower() for s in sensitive_keys):
                sanitized[k] = "***REDACTED***"
            else:
                sanitized[k] = v
        return sanitized


# =====================================================================
# 全局单例
# =====================================================================
_gm_instance: Optional[GovernanceManager] = None
_gm_lock = threading.Lock()


def get_governance_manager() -> GovernanceManager:
    """获取全局 GovernanceManager 单例。"""
    global _gm_instance
    if _gm_instance is None:
        with _gm_lock:
            if _gm_instance is None:
                _gm_instance = GovernanceManager()
    return _gm_instance


def reset_governance_manager() -> None:
    """测试用:重置全局单例。"""
    global _gm_instance
    with _gm_lock:
        _gm_instance = None
