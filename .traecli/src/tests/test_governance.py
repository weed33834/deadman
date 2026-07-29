"""P8.17-P8.27 AI 治理框架测试 - 9 模块覆盖。

覆盖:
    - ModelCard: register / get / list / update / archive + persistence
    - DataCard: register / get / list + sensitivity levels + sensitivity filter
    - RiskCard: register / assess_risk matrix / list_by_severity / mitigate / accept
    - TransparencyReport: generate / add_section / export (json/md/html) / list
    - AIRedline: is_allowed / enforce for each category + exception_role + RedlineViolation
    - Appeals: file / review / get / list_pending / list_by_user + SLA escalation
    - EthicsCommittee: register_member / submit / assign / decide + quorum check +
                       digital twin case consent
    - LiabilityInsurance: register_policy / file_claim / process_claim / get_coverage /
                          check_coverage
    - GovernanceManager: end-to-end + disabled state raises GovernanceDisabledError
"""

from __future__ import annotations

import json
import time

import pytest

# =====================================================================
# 全局 fixture - 启用 governance + 重置所有单例
# =====================================================================

@pytest.fixture(autouse=True)
def enable_governance(monkeypatch, tmp_path):
    """每个测试启用 governance + 重置全局单例 + 隔离数据目录。"""
    monkeypatch.setenv("DEADMAN_GOVERNANCE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_AI_REDLINE_BYPASS", "0")
    # 隔离数据目录(避免污染 ~/.deadman/)
    monkeypatch.setenv("DEADMAN_TENANTS_ROOT", str(tmp_path / "tenants"))
    monkeypatch.setenv("DEADMAN_DEFAULT_TENANT_ID", "default")
    monkeypatch.setenv("HOME", str(tmp_path))

    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0

    # 重置所有 governance 子模块的全局单例
    import deadman.governance.ai_redlines as arl_mod
    import deadman.governance.appeals as ap_mod
    import deadman.governance.data_card as dc_mod
    import deadman.governance.ethics_committee as ec_mod
    import deadman.governance.liability_insurance as li_mod
    import deadman.governance.manager as gm_mod
    import deadman.governance.model_card as mc_mod
    import deadman.governance.risk_card as rc_mod
    import deadman.governance.transparency as tr_mod
    mc_mod._mcr_instance = None
    dc_mod._dcr_instance = None
    rc_mod._ra_instance = None
    tr_mod._tr_instance = None
    arl_mod._arl_instance = None
    ap_mod._am_instance = None
    ec_mod._ec_instance = None
    li_mod._li_instance = None
    gm_mod._gm_instance = None

    yield

    # 测试后清理
    monkeypatch.setenv("DEADMAN_GOVERNANCE_ENABLED", "0")
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    mc_mod._mcr_instance = None
    dc_mod._dcr_instance = None
    rc_mod._ra_instance = None
    tr_mod._tr_instance = None
    arl_mod._arl_instance = None
    ap_mod._am_instance = None
    ec_mod._ec_instance = None
    li_mod._li_instance = None
    gm_mod._gm_instance = None


# =====================================================================
# 1. ModelCard
# =====================================================================

class TestModelCard:
    def test_register_and_get(self, tmp_path):
        from deadman.governance.model_card import ModelCard, ModelCardRegistry
        reg = ModelCardRegistry(store_path=tmp_path / "mc.json")
        card = ModelCard(
            model_id="memorial-v1",
            name="Memorial Writer",
            version="1.0.0",
            description="AI memorial letter writer",
            owner="team-ai",
            intended_use=["write memorial letters"],
            not_for_use=["legal advice"],
            capabilities=["empathetic writing"],
            limitations=["Chinese only"],
        )
        reg.register(card)
        got = reg.get("memorial-v1")
        assert got is not None
        assert got.name == "Memorial Writer"
        assert got.version == "1.0.0"
        assert "write memorial letters" in got.intended_use

    def test_list_all(self, tmp_path):
        from deadman.governance.model_card import ModelCard, ModelCardRegistry
        reg = ModelCardRegistry(store_path=tmp_path / "mc.json")
        reg.register(ModelCard(model_id="m1", name="M1"))
        reg.register(ModelCard(model_id="m2", name="M2"))
        all_cards = reg.list_all()
        assert len(all_cards) == 2
        ids = {c.model_id for c in all_cards}
        assert ids == {"m1", "m2"}

    def test_update_fields(self, tmp_path):
        from deadman.governance.model_card import ModelCard, ModelCardRegistry
        reg = ModelCardRegistry(store_path=tmp_path / "mc.json")
        reg.register(ModelCard(model_id="m1", name="M1", version="1.0.0"))
        updated = reg.update("m1", version="2.0.0", owner="new-team")
        assert updated is not None
        assert updated.version == "2.0.0"
        assert updated.owner == "new-team"
        # 未存在 → None
        assert reg.update("nonexistent", name="x") is None

    def test_archive(self, tmp_path):
        from deadman.governance.model_card import ModelCard, ModelCardRegistry
        reg = ModelCardRegistry(store_path=tmp_path / "mc.json")
        reg.register(ModelCard(model_id="m1", name="M1"))
        archived = reg.archive("m1")
        assert archived is not None
        assert archived.archived is True
        active = reg.list_active()
        assert len(active) == 0
        all_cards = reg.list_all()
        assert len(all_cards) == 1

    def test_persistence(self, tmp_path):
        from deadman.governance.model_card import ModelCard, ModelCardRegistry
        store = tmp_path / "mc.json"
        reg1 = ModelCardRegistry(store_path=store)
        reg1.register(ModelCard(model_id="m1", name="M1", version="1.0.0"))
        # 新实例加载同一文件
        reg2 = ModelCardRegistry(store_path=store)
        got = reg2.get("m1")
        assert got is not None
        assert got.name == "M1"
        assert got.version == "1.0.0"


# =====================================================================
# 2. DataCard
# =====================================================================

class TestDataCard:
    def test_register_and_get(self, tmp_path):
        from deadman.governance.data_card import DataCard, DataCardRegistry, SensitivityLevel
        reg = DataCardRegistry(store_path=tmp_path / "dc.json")
        card = DataCard(
            dataset_id="user-profiles",
            name="User Profiles",
            source="user_upload",
            collection_method="form",
            pii_categories=["name", "phone", "id_card"],
            consent_required=True,
            sensitivity_level=SensitivityLevel.CONFIDENTIAL,
        )
        reg.register(card)
        got = reg.get("user-profiles")
        assert got is not None
        assert got.sensitivity_level == SensitivityLevel.CONFIDENTIAL
        assert "id_card" in got.pii_categories

    def test_list_all(self, tmp_path):
        from deadman.governance.data_card import DataCard, DataCardRegistry
        reg = DataCardRegistry(store_path=tmp_path / "dc.json")
        reg.register(DataCard(dataset_id="d1", name="D1"))
        reg.register(DataCard(dataset_id="d2", name="D2"))
        assert len(reg.list_all()) == 2

    def test_sensitivity_levels(self, tmp_path):
        from deadman.governance.data_card import (
            DataCard,
            DataCardRegistry,
            SensitivityLevel,
        )
        reg = DataCardRegistry(store_path=tmp_path / "dc.json")
        reg.register(DataCard(
            dataset_id="public-docs",
            name="Public Docs",
            sensitivity_level=SensitivityLevel.PUBLIC,
        ))
        reg.register(DataCard(
            dataset_id="medical-records",
            name="Medical Records",
            sensitivity_level=SensitivityLevel.RESTRICTED,
        ))
        # 仅列出 >= CONFIDENTIAL
        confidential_plus = reg.list_by_sensitivity(SensitivityLevel.CONFIDENTIAL)
        assert len(confidential_plus) == 1
        assert confidential_plus[0].dataset_id == "medical-records"

    def test_sensitivity_requires_consent(self):
        from deadman.governance.data_card import SensitivityLevel
        assert SensitivityLevel.PUBLIC.requires_consent() is False
        assert SensitivityLevel.INTERNAL.requires_consent() is False
        assert SensitivityLevel.CONFIDENTIAL.requires_consent() is True
        assert SensitivityLevel.RESTRICTED.requires_consent() is True

    def test_persistence(self, tmp_path):
        from deadman.governance.data_card import DataCard, DataCardRegistry, SensitivityLevel
        store = tmp_path / "dc.json"
        reg1 = DataCardRegistry(store_path=store)
        reg1.register(DataCard(
            dataset_id="d1",
            name="D1",
            sensitivity_level=SensitivityLevel.RESTRICTED,
        ))
        reg2 = DataCardRegistry(store_path=store)
        got = reg2.get("d1")
        assert got is not None
        assert got.sensitivity_level == SensitivityLevel.RESTRICTED


# =====================================================================
# 3. RiskCard
# =====================================================================

class TestRiskCard:
    def test_register_and_get(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCard,
            RiskCategory,
            RiskLikelihood,
            RiskSeverity,
            RiskStatus,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        card = RiskCard(
            risk_id="R-001",
            title="PII 泄漏风险",
            category=RiskCategory.PRIVACY,
            severity=RiskSeverity.HIGH,
            likelihood=RiskLikelihood.LIKELY,
        )
        ra.register(card)
        got = ra.get("R-001")
        assert got is not None
        assert got.title == "PII 泄漏风险"
        assert got.status == RiskStatus.OPEN

    def test_assess_risk_matrix(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCategory,
            RiskLikelihood,
            RiskSeverity,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        # catastrophic × certain = 5×5 = 25 (max)
        score = ra.assess_risk(
            RiskCategory.SAFETY,
            RiskSeverity.CATASTROPHIC,
            RiskLikelihood.CERTAIN,
        )
        assert score.score == 25
        assert score.requires_ethics_committee is True
        assert score.requires_review is True
        assert score.level == "critical"

    def test_assess_risk_low(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCategory,
            RiskLikelihood,
            RiskSeverity,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        # low × rare = 1×1 = 1
        score = ra.assess_risk(
            RiskCategory.OPERATIONAL,
            RiskSeverity.LOW,
            RiskLikelihood.RARE,
        )
        assert score.score == 1
        assert score.requires_review is False
        assert score.requires_ethics_committee is False
        assert score.level == "low"

    def test_assess_risk_medium_threshold(self, tmp_path):
        from deadman.governance.risk_card import (
            ETHICS_COMMITTEE_THRESHOLD,
            REVIEW_THRESHOLD,
            RiskAssessment,
            RiskCategory,
            RiskLikelihood,
            RiskSeverity,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        # high × likely = 3×4 = 12 (medium level, no review)
        score = ra.assess_risk(
            RiskCategory.ETHICAL,
            RiskSeverity.HIGH,
            RiskLikelihood.LIKELY,
        )
        assert score.score == 12
        assert score.requires_review is False
        assert score.level == "medium"
        # critical × likely = 4×4 = 16 (review required, high level)
        score2 = ra.assess_risk(
            RiskCategory.LEGAL,
            RiskSeverity.CRITICAL,
            RiskLikelihood.LIKELY,
        )
        assert score2.score == 16
        assert score2.requires_review is True
        assert score2.requires_ethics_committee is False
        assert score2.level == "high"
        # 阈值常量
        assert REVIEW_THRESHOLD == 15
        assert ETHICS_COMMITTEE_THRESHOLD == 20

    def test_list_by_severity(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCard,
            RiskLikelihood,
            RiskSeverity,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        ra.register(RiskCard(
            risk_id="R-low", title="low", severity=RiskSeverity.LOW,
            likelihood=RiskLikelihood.RARE,
        ))
        ra.register(RiskCard(
            risk_id="R-high", title="high", severity=RiskSeverity.HIGH,
            likelihood=RiskLikelihood.LIKELY,
        ))
        ra.register(RiskCard(
            risk_id="R-crit", title="crit", severity=RiskSeverity.CRITICAL,
            likelihood=RiskLikelihood.CERTAIN,
        ))
        high_plus = ra.list_by_severity(RiskSeverity.HIGH)
        ids = {c.risk_id for c in high_plus}
        assert ids == {"R-high", "R-crit"}

    def test_list_by_status(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCard,
            RiskStatus,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        ra.register(RiskCard(risk_id="R1", title="t1", status=RiskStatus.OPEN))
        ra.register(RiskCard(risk_id="R2", title="t2", status=RiskStatus.ACCEPTED))
        open_cards = ra.list_by_status(RiskStatus.OPEN)
        assert len(open_cards) == 1
        assert open_cards[0].risk_id == "R1"

    def test_mitigate(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCard,
            RiskStatus,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        ra.register(RiskCard(risk_id="R1", title="t1"))
        mitigated = ra.mitigate("R1", "added input guard")
        assert mitigated is not None
        assert mitigated.status == RiskStatus.MITIGATING
        assert "input guard" in mitigated.mitigation_strategy

    def test_accept_requires_reason(self, tmp_path):
        from deadman.governance.risk_card import (
            RiskAssessment,
            RiskCard,
            RiskStatus,
        )
        ra = RiskAssessment(store_path=tmp_path / "rc.json")
        ra.register(RiskCard(risk_id="R1", title="t1"))
        # 空 reason 抛异常
        with pytest.raises(ValueError):
            ra.accept("R1", "")
        with pytest.raises(ValueError):
            ra.accept("R1", "   ")
        # 有效 reason
        accepted = ra.accept("R1", "low impact, monitoring in place")
        assert accepted.status == RiskStatus.ACCEPTED
        assert accepted.accepted_reason == "low impact, monitoring in place"


# =====================================================================
# 4. TransparencyReport
# =====================================================================

class TestTransparencyReport:
    def test_generate_report(self, tmp_path):
        from deadman.governance.transparency import (
            ReportPeriod,
            TransparencyReporter,
        )
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        report = reporter.generate_report(
            period_start=time.time() - 86400,
            period_end=time.time(),
            period=ReportPeriod.MONTHLY,
        )
        assert report.report_id.startswith("transparency-")
        assert report.period == ReportPeriod.MONTHLY
        assert report.period_end > report.period_start

    def test_add_section(self, tmp_path):
        from deadman.governance.transparency import TransparencyReporter
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        report = reporter.generate_report(time.time() - 100, time.time())
        updated = reporter.add_section(
            report.report_id, "executive_summary", "本月无重大事件"
        )
        assert updated is not None
        assert "executive_summary" in updated.sections
        assert updated.sections["executive_summary"] == "本月无重大事件"

    def test_export_json(self, tmp_path):
        from deadman.governance.transparency import TransparencyReporter
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        report = reporter.generate_report(time.time() - 100, time.time())
        exported = reporter.export(report.report_id, format="json")
        assert isinstance(exported, bytes)
        data = json.loads(exported.decode("utf-8"))
        assert data["report_id"] == report.report_id
        assert "period_start" in data

    def test_export_markdown(self, tmp_path):
        from deadman.governance.transparency import TransparencyReporter
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        report = reporter.generate_report(time.time() - 100, time.time())
        exported = reporter.export(report.report_id, format="markdown")
        md = exported.decode("utf-8")
        assert "# AI 透明度报告" in md
        assert "## 关键指标" in md
        assert report.report_id in md

    def test_export_html(self, tmp_path):
        from deadman.governance.transparency import TransparencyReporter
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        report = reporter.generate_report(time.time() - 100, time.time())
        exported = reporter.export(report.report_id, format="html")
        html = exported.decode("utf-8")
        assert "<!DOCTYPE html>" in html
        assert "<html>" in html
        assert "AI 透明度报告" in html

    def test_export_invalid_format(self, tmp_path):
        from deadman.governance.transparency import TransparencyReporter
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        report = reporter.generate_report(time.time() - 100, time.time())
        with pytest.raises(ValueError):
            reporter.export(report.report_id, format="pdf")

    def test_export_nonexistent_report(self, tmp_path):
        from deadman.governance.transparency import TransparencyReporter
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        with pytest.raises(KeyError):
            reporter.export("nonexistent", format="json")

    def test_list_reports(self, tmp_path):
        from deadman.governance.transparency import (
            ReportPeriod,
            TransparencyReporter,
        )
        reporter = TransparencyReporter(store_path=tmp_path / "tr.json")
        reporter.generate_report(time.time() - 100, time.time(), period=ReportPeriod.MONTHLY)
        reporter.generate_report(time.time() - 100, time.time(), period=ReportPeriod.QUARTERLY)
        all_reports = reporter.list_reports()
        assert len(all_reports) == 2
        monthly = reporter.list_reports(period=ReportPeriod.MONTHLY)
        assert len(monthly) == 1
        assert monthly[0].period == ReportPeriod.MONTHLY


# =====================================================================
# 5. AIRedline
# =====================================================================

class TestAIRedline:
    def test_list_redlines_returns_all_categories(self):
        from deadman.governance.ai_redlines import AIRedline, RedlineCategory
        redline = AIRedline()
        rules = redline.list_redlines()
        assert len(rules) == 7
        categories = {r.category for r in rules}
        assert categories == set(RedlineCategory)

    def test_sign_will_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed("代签遗嘱", context={"role": "user"})
        assert result.allowed is False
        assert result.requires_human is True

    def test_sign_contract_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed("sign_contract", context={"role": "user"})
        assert result.allowed is False

    def test_medical_decision_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed("medical_decision", context={"role": "user"})
        assert result.allowed is False

    def test_legal_decision_advisory_only_allowed(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        # advisory_only context 允许
        result = redline.is_allowed(
            "legal_decision",
            context={"role": "admin", "advisory_only": True},
        )
        assert result.allowed is True

    def test_legal_decision_without_context_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed(
            "legal_decision",
            context={"role": "user"},
        )
        assert result.allowed is False

    def test_financial_transfer_admin_audit_allowed(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed(
            "financial_transfer",
            context={"role": "admin", "admin_audit": True},
        )
        assert result.allowed is True

    def test_financial_transfer_user_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        # 即使有 admin_audit context,user 角色仍禁止
        result = redline.is_allowed(
            "financial_transfer",
            context={"role": "user", "admin_audit": True},
        )
        assert result.allowed is False

    def test_death_determination_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed("death_determination", context={"role": "admin"})
        assert result.allowed is False  # 永远禁止

    def test_guardian_assignment_blocked(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed("guardian_assignment", context={"role": "admin"})
        assert result.allowed is False

    def test_enforce_raises_violation(self):
        from deadman.governance.ai_redlines import (
            AIRedline,
            RedlineCategory,
            RedlineViolation,
        )
        redline = AIRedline()
        with pytest.raises(RedlineViolation) as exc_info:
            redline.enforce("代签遗嘱", context={"role": "user"})
        assert exc_info.value.category == RedlineCategory.SIGN_WILL
        assert "代签遗嘱" in str(exc_info.value)

    def test_enforce_passes_when_allowed(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        # 未命中红线 → 允许,不抛
        redline.enforce("write a memorial letter", context={"role": "user"})

    def test_non_redline_action_allowed(self):
        from deadman.governance.ai_redlines import AIRedline
        redline = AIRedline()
        result = redline.is_allowed("write a poem", context={"role": "user"})
        assert result.allowed is True
        assert result.category is None

    def test_bypass_env_only_for_test(self, monkeypatch):
        from deadman.governance.ai_redlines import AIRedline
        monkeypatch.setenv("DEADMAN_AI_REDLINE_BYPASS", "1")
        redline = AIRedline()
        result = redline.is_allowed("代签遗嘱", context={"role": "user"})
        assert result.allowed is True


# =====================================================================
# 6. Appeals
# =====================================================================

class TestAppeals:
    def test_file_and_get(self, tmp_path):
        from deadman.governance.appeals import AppealsManager, AppealStatus
        am = AppealsManager(store_path=tmp_path / "ap.json")
        appeal = am.file(user_id="u1", decision_id="d1", reason="content wrong")
        assert appeal.status == AppealStatus.FILED
        assert appeal.user_id == "u1"
        assert appeal.decision_id == "d1"
        got = am.get(appeal.appeal_id)
        assert got is not None
        assert got.reason == "content wrong"

    def test_review_approved(self, tmp_path):
        from deadman.governance.appeals import (
            AppealDecision,
            AppealsManager,
            AppealStatus,
        )
        am = AppealsManager(store_path=tmp_path / "ap.json")
        appeal = am.file("u1", "d1", "wrong")
        reviewed = am.review(
            appeal.appeal_id,
            reviewer_id="rev1",
            decision=AppealDecision.APPROVED,
            resolution_text="decision reversed",
        )
        assert reviewed.status == AppealStatus.APPROVED
        assert reviewed.reviewer_id == "rev1"
        assert reviewed.reviewed_at is not None

    def test_review_rejected(self, tmp_path):
        from deadman.governance.appeals import (
            AppealDecision,
            AppealsManager,
            AppealStatus,
        )
        am = AppealsManager(store_path=tmp_path / "ap.json")
        appeal = am.file("u1", "d1", "wrong")
        reviewed = am.review(
            appeal.appeal_id,
            reviewer_id="rev1",
            decision=AppealDecision.REJECTED,
            resolution_text="maintained",
        )
        assert reviewed.status == AppealStatus.REJECTED

    def test_list_pending(self, tmp_path):
        from deadman.governance.appeals import (
            AppealDecision,
            AppealsManager,
        )
        am = AppealsManager(store_path=tmp_path / "ap.json")
        a1 = am.file("u1", "d1", "r1")
        a2 = am.file("u2", "d2", "r2")
        am.review(a2.appeal_id, "rev1", AppealDecision.APPROVED, "ok")
        pending = am.list_pending()
        assert len(pending) == 1
        assert pending[0].appeal_id == a1.appeal_id

    def test_list_by_user(self, tmp_path):
        from deadman.governance.appeals import AppealsManager
        am = AppealsManager(store_path=tmp_path / "ap.json")
        am.file("u1", "d1", "r1")
        am.file("u1", "d2", "r2")
        am.file("u2", "d3", "r3")
        u1_appeals = am.list_by_user("u1")
        assert len(u1_appeals) == 2
        for a in u1_appeals:
            assert a.user_id == "u1"

    def test_sla_escalation(self, tmp_path):
        from deadman.governance.appeals import (
            APPEAL_SLA_SECONDS,
            AppealsManager,
            AppealStatus,
        )
        am = AppealsManager(store_path=tmp_path / "ap.json")
        appeal = am.file("u1", "d1", "r1")
        # 模拟 SLA 超时 - 手动改 filed_at 到 8 天前
        # 通过 list_overdue 触发检查
        # 先直接修改 cache 中的 appeal
        am._cache[appeal.appeal_id].filed_at = time.time() - (APPEAL_SLA_SECONDS + 100)
        am._cache[appeal.appeal_id].sla_deadline = (
            am._cache[appeal.appeal_id].filed_at + APPEAL_SLA_SECONDS
        )
        am._save()
        # 重新加载 (走 _check_sla_escalations)
        am2 = AppealsManager(store_path=tmp_path / "ap.json")
        am2.list_pending()
        # 应该已升级,status = ESCALATED
        escalated = [a for a in am2.list_all() if a.escalated]
        assert len(escalated) == 1
        assert escalated[0].status == AppealStatus.ESCALATED

    def test_persistence(self, tmp_path):
        from deadman.governance.appeals import AppealsManager
        store = tmp_path / "ap.json"
        am1 = AppealsManager(store_path=store)
        appeal = am1.file("u1", "d1", "reason")
        am2 = AppealsManager(store_path=store)
        got = am2.get(appeal.appeal_id)
        assert got is not None
        assert got.user_id == "u1"


# =====================================================================
# 7. EthicsCommittee
# =====================================================================

class TestEthicsCommittee:
    def _setup_committee(self, ec):
        from deadman.governance.ethics_committee import (
            CommitteeMember,
            MemberRole,
        )
        ec.register_member(CommitteeMember(
            member_id="chair-1", name="主席", role=MemberRole.CHAIR,
        ))
        ec.register_member(CommitteeMember(
            member_id="lawyer-1", name="律师", role=MemberRole.LAWYER,
        ))
        ec.register_member(CommitteeMember(
            member_id="eth-1", name="伦理学者", role=MemberRole.ETHICIST,
        ))
        ec.register_member(CommitteeMember(
            member_id="eng-1", name="工程师", role=MemberRole.ENGINEER,
        ))
        ec.register_member(CommitteeMember(
            member_id="user-1", name="用户代表", role=MemberRole.USER_REP,
        ))

    def test_register_member(self, tmp_path):
        from deadman.governance.ethics_committee import (
            CommitteeMember,
            EthicsCommittee,
            MemberRole,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        ec.register_member(CommitteeMember(
            member_id="m1", name="张三", role=MemberRole.CHAIR, expertise=["law"],
        ))
        members = ec.list_members()
        assert len(members) == 1
        assert members[0].role == MemberRole.CHAIR

    def test_submit_case(self, tmp_path):
        from deadman.governance.ethics_committee import (
            CaseStatus,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        case = ec.submit_case(
            title="test case",
            description="desc",
            category="bias_dispute",
            severity="medium",
        )
        assert case.status == CaseStatus.SUBMITTED
        assert case.title == "test case"
        assert case.category == "bias_dispute"

    def test_assign_low_severity_no_quorum_required(self, tmp_path):
        from deadman.governance.ethics_committee import (
            CaseStatus,
            CommitteeMember,
            EthicsCommittee,
            MemberRole,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        ec.register_member(CommitteeMember(
            member_id="m1", name="M1", role=MemberRole.ETHICIST,
        ))
        case = ec.submit_case("low case", "desc", "general", "low")
        assigned = ec.assign(case.case_id, ["m1"])
        assert assigned.status == CaseStatus.ASSIGNED

    def test_assign_high_severity_quorum_chair_required(self, tmp_path):
        from deadman.governance.ethics_committee import (
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case("high case", "desc", "bias", "high")
        # 缺 chair → 失败
        with pytest.raises(ValueError, match="chair"):
            ec.assign(case.case_id, ["lawyer-1", "eth-1", "eng-1"])

    def test_assign_high_severity_quorum_lawyer_required(self, tmp_path):
        from deadman.governance.ethics_committee import EthicsCommittee
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case("high case", "desc", "bias", "high")
        # 缺 lawyer → 失败
        with pytest.raises(ValueError, match="lawyer"):
            ec.assign(case.case_id, ["chair-1", "eth-1", "eng-1"])

    def test_assign_high_severity_quorum_three_members_required(self, tmp_path):
        from deadman.governance.ethics_committee import EthicsCommittee
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case("high case", "desc", "bias", "high")
        # 只有 2 人,不够 3 人 → 失败
        with pytest.raises(ValueError, match="at least 3"):
            ec.assign(case.case_id, ["chair-1", "lawyer-1"])

    def test_assign_high_severity_quorum_satisfied(self, tmp_path):
        from deadman.governance.ethics_committee import (
            CaseStatus,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case("high case", "desc", "bias", "high")
        assigned = ec.assign(case.case_id, ["chair-1", "lawyer-1", "eth-1"])
        assert assigned.status == CaseStatus.ASSIGNED

    def test_decide(self, tmp_path):
        from deadman.governance.ethics_committee import (
            CaseDecision,
            CaseStatus,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case("case", "desc", "general", "high")
        ec.assign(case.case_id, ["chair-1", "lawyer-1", "eth-1"])
        decided = ec.decide(
            case.case_id,
            CaseDecision.APPROVED_WITH_CONDITIONS,
            "批准但需定期审计",
        )
        assert decided.status == CaseStatus.DECIDED
        assert decided.decision == CaseDecision.APPROVED_WITH_CONDITIONS
        assert decided.decision_date is not None

    def test_digital_twin_case_requires_user_rep(self, tmp_path):
        from deadman.governance.ethics_committee import (
            DIGITAL_TWIN_DECEASED_CATEGORY,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case(
            "数字孪生逝者",
            "用户希望生成逝者语音",
            DIGITAL_TWIN_DECEASED_CATEGORY,
            "high",
        )
        ec.verify_user_consent(case.case_id)
        # 无 user_rep → 失败
        with pytest.raises(ValueError, match="user_rep"):
            ec.assign(case.case_id, ["chair-1", "lawyer-1", "eth-1"])

    def test_digital_twin_case_requires_consent_verification(self, tmp_path):
        from deadman.governance.ethics_committee import (
            DIGITAL_TWIN_DECEASED_CATEGORY,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case(
            "数字孪生逝者",
            "用户希望生成逝者语音",
            DIGITAL_TWIN_DECEASED_CATEGORY,
            "high",
        )
        # 未验证 consent → 失败
        with pytest.raises(ValueError, match="user consent"):
            ec.assign(case.case_id, ["chair-1", "lawyer-1", "user-1"])

    def test_digital_twin_case_full_flow(self, tmp_path):
        from deadman.governance.ethics_committee import (
            DIGITAL_TWIN_DECEASED_CATEGORY,
            CaseDecision,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        self._setup_committee(ec)
        case = ec.submit_case(
            "数字孪生逝者",
            "用户希望生成逝者语音",
            DIGITAL_TWIN_DECEASED_CATEGORY,
            "high",
        )
        ec.verify_user_consent(case.case_id)
        assigned = ec.assign(case.case_id, ["chair-1", "lawyer-1", "user-1"])
        assert len(assigned.assigned_members) == 3
        decided = ec.decide(case.case_id, CaseDecision.APPROVED, "已审议通过")
        assert decided.decision == CaseDecision.APPROVED

    def test_list_cases_by_status(self, tmp_path):
        from deadman.governance.ethics_committee import (
            CaseStatus,
            EthicsCommittee,
        )
        ec = EthicsCommittee(store_path=tmp_path / "ec.json")
        ec.submit_case("c1", "d1", "general", "low")
        ec.submit_case("c2", "d2", "general", "low")
        submitted = ec.list_cases(status=CaseStatus.SUBMITTED)
        assert len(submitted) == 2


# =====================================================================
# 8. LiabilityInsurance
# =====================================================================

class TestLiabilityInsurance:
    def _make_policy(self, policy_id="P-001", coverage_amount=1_000_000):
        from deadman.governance.liability_insurance import InsurancePolicy
        return InsurancePolicy(
            policy_id=policy_id,
            provider="PICC",
            coverage_amount=coverage_amount,
            deductible=10_000,
            coverage_types=["data_leak", "legal_advice_error"],
            exclusions=["intentional_misconduct"],
            start_date=time.time() - 86400,
            end_date=time.time() + 86400 * 365,
            premium=50_000,
        )

    def test_register_policy(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        policy = self._make_policy()
        li.register_policy(policy)
        got = li.get_policy("P-001")
        assert got is not None
        assert got.provider == "PICC"

    def test_file_claim(self, tmp_path):
        from deadman.governance.liability_insurance import (
            ClaimStatus,
            LiabilityInsurance,
        )
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy())
        claim = li.file_claim({
            "policy_id": "P-001",
            "user_id": "u1",
            "incident_description": "wrong legal advice",
            "amount_claimed": 50000,
            "coverage_type": "legal_advice_error",
        })
        assert claim.status == ClaimStatus.FILED
        assert claim.amount_claimed == 50000

    def test_file_claim_policy_not_found(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        with pytest.raises(ValueError, match="Policy not found"):
            li.file_claim({
                "policy_id": "nonexistent",
                "user_id": "u1",
            })

    def test_process_claim_approved(self, tmp_path):
        from deadman.governance.liability_insurance import (
            ClaimStatus,
            LiabilityInsurance,
        )
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy())
        claim = li.file_claim({
            "policy_id": "P-001", "user_id": "u1", "amount_claimed": 50000,
        })
        processed = li.process_claim(
            claim.claim_id,
            ClaimStatus.APPROVED,
            payout=45000,
            reviewer_id="rev1",
            resolution_text="approved with 10% deduction",
        )
        assert processed.status == ClaimStatus.APPROVED
        assert processed.payout_amount == 45000
        assert processed.resolved_at is not None

    def test_process_claim_rejected(self, tmp_path):
        from deadman.governance.liability_insurance import (
            ClaimStatus,
            LiabilityInsurance,
        )
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy())
        claim = li.file_claim({"policy_id": "P-001", "user_id": "u1"})
        processed = li.process_claim(
            claim.claim_id, ClaimStatus.REJECTED, resolution_text="out of coverage"
        )
        assert processed.status == ClaimStatus.REJECTED
        assert processed.payout_amount == 0.0

    def test_get_coverage(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy(coverage_amount=1_000_000))
        summary = li.get_coverage("P-001")
        assert summary["found"] is True
        assert summary["provider"] == "PICC"
        assert summary["coverage_amount"] == 1_000_000
        assert summary["remaining_coverage"] == 1_000_000
        assert "data_leak" in summary["coverage_types"]

    def test_get_coverage_nonexistent(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        summary = li.get_coverage("nonexistent")
        assert summary["found"] is False

    def test_check_coverage_positive(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy(coverage_amount=1_000_000))
        assert li.check_coverage("data_leak", 100_000) is True

    def test_check_coverage_amount_exceeds(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy(coverage_amount=100_000))
        # 索赔金额超过保额 → False
        assert li.check_coverage("data_leak", 200_000) is False

    def test_check_coverage_wrong_type(self, tmp_path):
        from deadman.governance.liability_insurance import LiabilityInsurance
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        li.register_policy(self._make_policy())
        # ip_infringement 不在覆盖类型内
        assert li.check_coverage("ip_infringement", 100) is False

    def test_check_coverage_exclusion(self, tmp_path):
        from deadman.governance.liability_insurance import (
            LiabilityInsurance,
        )
        li = LiabilityInsurance(store_path=tmp_path / "li.json")
        # 加排除:data_leak 被 excluded
        policy = self._make_policy()
        policy.exclusions = ["data_leak"]
        li.register_policy(policy)
        assert li.check_coverage("data_leak", 100) is False


# =====================================================================
# 9. GovernanceManager end-to-end
# =====================================================================

class TestGovernanceManager:
    def test_disabled_state_raises(self, monkeypatch, tmp_path):
        # 关闭 governance → 资产注册抛 GovernanceDisabledError
        monkeypatch.setenv("DEADMAN_GOVERNANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.governance import (
            GovernanceDisabledError,
            get_governance_manager,
            reset_governance_manager,
        )
        reset_governance_manager()
        gm = get_governance_manager()
        from deadman.governance.model_card import ModelCard
        with pytest.raises(GovernanceDisabledError):
            gm.register_model(ModelCard(model_id="m1", name="M1"))

    def test_register_model_via_manager(self):
        from deadman.governance import get_governance_manager
        from deadman.governance.model_card import ModelCard
        gm = get_governance_manager()
        card = gm.register_model(ModelCard(model_id="m1", name="M1"))
        assert card.model_id == "m1"
        assert gm.model_cards.get("m1") is not None

    def test_register_dataset_via_manager(self):
        from deadman.governance import get_governance_manager
        from deadman.governance.data_card import DataCard, SensitivityLevel
        gm = get_governance_manager()
        card = gm.register_dataset(DataCard(
            dataset_id="d1", name="D1",
            sensitivity_level=SensitivityLevel.CONFIDENTIAL,
        ))
        assert card.dataset_id == "d1"

    def test_register_risk_via_manager(self):
        from deadman.governance import get_governance_manager
        from deadman.governance.risk_card import (
            RiskCard,
            RiskCategory,
            RiskSeverity,
        )
        gm = get_governance_manager()
        card = gm.register_risk(RiskCard(
            risk_id="R1", title="test",
            category=RiskCategory.PRIVACY, severity=RiskSeverity.HIGH,
        ))
        assert card.risk_id == "R1"

    def test_before_action_allows_normal_action(self):
        from deadman.governance import get_governance_manager
        gm = get_governance_manager()
        decision = gm.before_action("write a poem", context={"role": "user"})
        assert decision.allowed is True
        assert decision.redline_result is not None
        assert decision.redline_result.allowed is True

    def test_before_action_blocks_redline(self):
        from deadman.governance import get_governance_manager
        gm = get_governance_manager()
        decision = gm.before_action("代签遗嘱", context={"role": "user"})
        assert decision.allowed is False
        assert decision.redline_result is not None
        assert decision.redline_result.allowed is False

    def test_before_action_with_insurance_check(self):
        from deadman.governance import get_governance_manager
        from deadman.governance.liability_insurance import InsurancePolicy
        gm = get_governance_manager()
        # 注册保单
        gm.insurance.register_policy(InsurancePolicy(
            policy_id="P1", provider="PICC", coverage_amount=1_000_000,
            coverage_types=["data_leak"],
            start_date=time.time() - 86400, end_date=time.time() + 86400,
        ))
        # 调用 before_action 带保险预检
        decision = gm.before_action(
            "data export operation",
            context={"role": "user"},
            incident_type="data_leak",
            amount=100_000,
        )
        assert decision.allowed is True
        assert decision.insurance_covered is True

    def test_after_decision_increments_counters(self):
        from deadman.governance import get_governance_manager
        gm = get_governance_manager()
        gm.after_decision("d1", "u1", "AI generated content", model_id="m1", is_ai=True)
        gm.after_decision("d2", "u2", "Human review", is_ai=False)
        with gm._lock:
            assert gm._decision_count == 2
            assert gm._ai_decision_count == 1
            assert gm._human_review_count == 1
            assert gm._model_usage.get("m1") == 1

    def test_file_appeal_via_manager(self):
        from deadman.governance import get_governance_manager
        gm = get_governance_manager()
        appeal = gm.file_appeal("u1", "d1", "wrong content")
        assert appeal.appeal_id.startswith("appeal-")
        assert appeal.user_id == "u1"

    def test_generate_transparency_report_via_manager(self):
        from deadman.governance import get_governance_manager
        gm = get_governance_manager()
        # 先记录几个决策
        gm.after_decision("d1", "u1", "AI content", model_id="m1", is_ai=True)
        gm.after_decision("d2", "u2", "Human review", is_ai=False)
        report = gm.generate_transparency_report(
            period_start=time.time() - 86400,
            period_end=time.time(),
        )
        assert report.total_decisions >= 2
        assert report.ai_decisions_count >= 1
        assert report.human_review_count >= 1
        assert report.model_usage_stats.get("m1", 0) >= 1

    def test_submit_ethics_case_via_manager(self):
        from deadman.governance import get_governance_manager
        gm = get_governance_manager()
        case = gm.submit_ethics_case(
            "test case", "description", "general", "medium"
        )
        assert case.title == "test case"
        assert case.category == "general"

    def test_full_e2e_flow(self):
        """端到端:注册资产 → 决策前检查 → 决策后记录 → 复议 → 报告。"""
        from deadman.governance import get_governance_manager
        from deadman.governance.appeals import AppealDecision
        from deadman.governance.data_card import DataCard, SensitivityLevel
        from deadman.governance.model_card import ModelCard
        from deadman.governance.risk_card import (
            RiskCard,
            RiskCategory,
            RiskLikelihood,
            RiskSeverity,
        )

        gm = get_governance_manager()
        # 1. 注册资产
        gm.register_model(ModelCard(model_id="m1", name="Memorial", version="1.0"))
        gm.register_dataset(DataCard(
            dataset_id="d1", name="User Data",
            sensitivity_level=SensitivityLevel.CONFIDENTIAL,
        ))
        gm.register_risk(RiskCard(
            risk_id="R1", title="PII risk",
            category=RiskCategory.PRIVACY,
            severity=RiskSeverity.HIGH,
            likelihood=RiskLikelihood.POSSIBLE,
        ))

        # 2. 决策前检查 (允许)
        decision = gm.before_action("write memorial letter", context={"role": "user"})
        assert decision.allowed is True

        # 3. 决策前检查 (禁止 - 红线)
        blocked = gm.before_action("代签遗嘱", context={"role": "user"})
        assert blocked.allowed is False

        # 4. 决策后记录
        gm.after_decision("dec-1", "u1", "memorial content", model_id="m1")

        # 5. 用户复议
        appeal = gm.file_appeal("u1", "dec-1", "content not empathetic")
        gm.appeals.review(
            appeal.appeal_id, "rev1",
            AppealDecision.APPROVED, "content revised",
        )

        # 6. 生成透明度报告
        report = gm.generate_transparency_report(
            time.time() - 86400, time.time(),
        )
        assert report.total_decisions >= 1
