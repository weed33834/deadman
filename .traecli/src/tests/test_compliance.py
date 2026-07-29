"""P8.6 合规与监管测试 - 6 模块覆盖。

覆盖:
    - data_residency: 区域策略 / 跨境校验 / 敏感数据专属
    - right_to_delete: 请求 / 7 天宽限 / 跨存储清理 / 失败重试
    - ai_labeling: 可见声明 / 隐式水印 / 元数据 / 双重标记
    - audit_report: 事件记录 / 报告生成 / 多通道上报
    - retention: 策略 / 过期扫描 / 处置动作 / 租户覆盖
    - consent: 同意 / 撤回 / 版本控制 / 撤回不影响历史
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def enable_compliance(monkeypatch):
    """每个测试启用 compliance(默认关闭)。"""
    monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    # 重置全局单例
    import deadman.compliance as comp_pkg
    comp_pkg._dr_instance = None
    comp_pkg._rtd_instance = None
    comp_pkg._al_instance = None
    comp_pkg._ar_instance = None
    comp_pkg._rm_instance = None
    comp_pkg._cm_instance = None
    yield
    # 测试后关闭(避免污染其他测试)
    monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    comp_pkg._dr_instance = None
    comp_pkg._rtd_instance = None
    comp_pkg._al_instance = None
    comp_pkg._ar_instance = None
    comp_pkg._rm_instance = None
    comp_pkg._cm_instance = None


# =====================================================================
# 1. data_residency
# =====================================================================

class TestDataResidency:
    def test_set_policy_for_tenant(self, tmp_path):
        from deadman.compliance.data_residency import DataResidency, DataRegion
        dr = DataResidency(store_path=tmp_path / "residency.yaml")
        dr.set_policy(
            tenant_id="t1",
            primary_region=DataRegion.CN,
            allowed_regions=[DataRegion.HK],
        )
        # 重新加载验证持久化
        dr2 = DataResidency(store_path=tmp_path / "residency.yaml")
        policy = dr2.get_policy("t1")
        assert policy is not None
        assert policy.primary_region == DataRegion.CN

    def test_cross_border_violation(self, tmp_path):
        from deadman.compliance.data_residency import (
            DataResidency,
            ResidencyViolation,
        )
        from deadman.infrastructure.multi_tenant import TenantContext, TenantInfo
        dr = DataResidency(store_path=tmp_path / "residency.yaml")
        dr.set_policy(
            tenant_id="t1",
            primary_region="cn",
            allowed_regions=["cn"],  # 只允许 cn
            cross_border_consent=False,
        )
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            with pytest.raises(ResidencyViolation):
                dr.ensure_in_region("data", target_region="us", data_kind="user_profile")

    def test_cross_border_with_consent_allowed(self, tmp_path):
        from deadman.compliance.data_residency import DataResidency
        from deadman.infrastructure.multi_tenant import TenantContext, TenantInfo
        dr = DataResidency(store_path=tmp_path / "residency.yaml")
        dr.set_policy(
            tenant_id="t1",
            primary_region="cn",
            allowed_regions=["us"],
            cross_border_consent=True,
        )
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            # 不应抛异常
            dr.ensure_in_region("data", target_region="us")

    def test_sensitive_data_region(self, tmp_path):
        from deadman.compliance.data_residency import (
            DataResidency,
            ResidencyViolation,
        )
        from deadman.infrastructure.multi_tenant import TenantContext, TenantInfo
        dr = DataResidency(store_path=tmp_path / "residency.yaml")
        dr.set_policy(
            tenant_id="t1",
            primary_region="cn",
            allowed_regions=["us"],
            cross_border_consent=True,
            sensitive_data_regions={"financial": "cn"},
        )
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            # 财务数据必须留在 CN,即使 us 在 allowed_regions
            with pytest.raises(ResidencyViolation) as exc_info:
                dr.ensure_in_region("data", target_region="us", data_kind="financial")
            assert exc_info.value.data_kind == "financial"

    def test_disabled_skips_check(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.compliance.data_residency import DataResidency
        dr = DataResidency(store_path=tmp_path / "residency.yaml")
        # 关闭后不抛异常
        dr.ensure_in_region("data", target_region="us")


# =====================================================================
# 2. right_to_delete
# =====================================================================

class TestRightToDelete:
    def test_request_deletion(self, tmp_path):
        from deadman.compliance.right_to_delete import RightToDelete, DeletionStatus
        rtd = RightToDelete(store_path=tmp_path / "deletions.json")
        req = rtd.request_deletion("user1", reason="user_left")
        assert req.user_id == "user1"
        assert req.status == DeletionStatus.REQUESTED
        # 7 天宽限
        assert req.scheduled_at > req.requested_at + 6 * 86400

    def test_execute_calls_deletors(self, tmp_path):
        from deadman.compliance.right_to_delete import RightToDelete, DeletionStatus
        rtd = RightToDelete(store_path=tmp_path / "deletions.json")
        called = []
        rtd.register_deletor("memory", lambda uid: called.append(("memory", uid)) or True)
        rtd.register_deletor("vector", lambda uid: called.append(("vector", uid)) or True)
        req = rtd.request_deletion("user1")
        result = rtd.execute(req.request_id)
        assert result.status == DeletionStatus.COMPLETED
        assert ("memory", "user1") in called
        assert ("vector", "user1") in called
        assert "audit_log" in result.stores_skipped

    def test_partial_failure_marks_failed(self, tmp_path):
        from deadman.compliance.right_to_delete import RightToDelete, DeletionStatus
        rtd = RightToDelete(store_path=tmp_path / "deletions.json")
        rtd.register_deletor("good", lambda uid: True)
        rtd.register_deletor("bad", lambda uid: (_ for _ in ()).throw(RuntimeError("disk error")))
        req = rtd.request_deletion("user1")
        result = rtd.execute(req.request_id)
        assert result.status == DeletionStatus.FAILED
        assert "good" in result.stores_processed
        assert "bad" in result.stores_failed

    def test_disabled_returns_completed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.compliance.right_to_delete import RightToDelete, DeletionStatus
        rtd = RightToDelete(store_path=tmp_path / "deletions.json")
        req = rtd.request_deletion("user1")
        assert req.status == DeletionStatus.COMPLETED


# =====================================================================
# 3. ai_labeling
# =====================================================================

class TestAILabeling:
    def test_label_adds_visible_text(self, tmp_path):
        from deadman.compliance.ai_labeling import AILabeling, LabelType
        labeling = AILabeling(store_path=tmp_path / "labels.jsonl")
        result = labeling.label("Hello world", user_id="u1", model="gpt-4o")
        assert LabelType.VISIBLE_TEXT in result.labels_applied
        assert "AI" in result.labeled_text or "AI 生成" in result.labeled_text

    def test_label_adds_metadata(self, tmp_path):
        from deadman.compliance.ai_labeling import AILabeling, LabelType, METADATA_AI_FLAG
        labeling = AILabeling(store_path=tmp_path / "labels.jsonl")
        result = labeling.label("Hello", user_id="u1", model="gpt-4o")
        assert LabelType.METADATA in result.labels_applied
        assert result.metadata[METADATA_AI_FLAG] is True
        assert result.metadata["ai_model"] == "gpt-4o"

    def test_label_adds_watermark(self, tmp_path):
        from deadman.compliance.ai_labeling import AILabeling, LabelType
        labeling = AILabeling(store_path=tmp_path / "labels.jsonl")
        result = labeling.label("Hello", user_id="u1", model="gpt-4o")
        assert LabelType.IMPLICIT_WATERMARK in result.labels_applied
        assert result.watermark_hash
        assert len(result.watermark_hash) == 16

    def test_watermark_lookup(self, tmp_path):
        from deadman.compliance.ai_labeling import AILabeling
        labeling = AILabeling(store_path=tmp_path / "labels.jsonl")
        result = labeling.label("Hello", user_id="u1", model="gpt-4o")
        # 反查指纹
        info = labeling.lookup_fingerprint(result.watermark_hash)
        assert info is not None
        assert info["user_id"] == "u1"
        assert info["model"] == "gpt-4o"

    def test_verify_labels(self, tmp_path):
        from deadman.compliance.ai_labeling import AILabeling
        labeling = AILabeling(store_path=tmp_path / "labels.jsonl")
        result = labeling.label("Hello", user_id="u1", model="gpt-4o")
        verification = labeling.verify(result.labeled_text, result.metadata)
        assert verification["visible_label"] is True
        assert verification["metadata"] is True
        assert verification["watermark"] is True

    def test_disabled_only_adds_minimal_flag(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.compliance.ai_labeling import AILabeling, METADATA_AI_FLAG
        labeling = AILabeling(store_path=tmp_path / "labels.jsonl")
        result = labeling.label("Hello", user_id="u1")
        assert result.labeled_text == "Hello"  # 不加可见声明
        assert result.metadata[METADATA_AI_FLAG] is True

    def test_dual_label_requirement_warning(self, tmp_path, caplog):
        from deadman.compliance.ai_labeling import AILabeling, LabelingConfig
        # 关闭 metadata 测试警告
        cfg = LabelingConfig(enable_metadata=False, require_dual_label=True)
        labeling = AILabeling(config=cfg, store_path=tmp_path / "labels.jsonl")
        with caplog.at_level("WARNING"):
            labeling.label("Hello", user_id="u1", model="gpt-4o")
        assert any("Dual label requirement not met" in r.message for r in caplog.records)


# =====================================================================
# 4. audit_report
# =====================================================================

class TestAuditReport:
    def test_record_event(self, tmp_path):
        from deadman.compliance.audit_report import AuditReporter, AuditEvent
        reporter = AuditReporter(store_path=tmp_path / "audit.json")
        reporter.record_event(AuditEvent(
            timestamp=time.time(),
            event_type="data_leak",
            severity="critical",
            description="Test leak",
        ))
        # 事件已记录
        assert len(reporter._events) == 1

    def test_generate_report_aggregates_events(self, tmp_path):
        from deadman.compliance.audit_report import (
            AuditReporter,
            AuditEvent,
            ReportFrequency,
            ReportStatus,
        )
        reporter = AuditReporter(store_path=tmp_path / "audit.json")
        now = time.time()
        # 记录 3 个事件
        reporter.record_event(AuditEvent(
            timestamp=now - 100, event_type="residency_violation",
            severity="warning", description="cross border",
        ))
        reporter.record_event(AuditEvent(
            timestamp=now - 50, event_type="deletion_request",
            severity="info", description="user1 deleted",
        ))
        reporter.record_event(AuditEvent(
            timestamp=now - 30, event_type="deletion_completed",
            severity="info", description="user1 done",
        ))
        report = reporter.generate_report(
            period_start=now - 200,
            period_end=now,
            frequency=ReportFrequency.MONTHLY,
        )
        assert report.status == ReportStatus.DRAFT
        assert len(report.events) == 3
        assert report.residency_violations == 1
        assert report.deletion_requests == 1
        assert report.deletion_completed == 1

    def test_submit_via_file_channel(self, tmp_path, monkeypatch):
        from deadman.compliance.audit_report import (
            AuditReporter,
            ReportStatus,
        )
        monkeypatch.setenv("DEADMAN_AUDIT_SUBMIT_CHANNEL", "file")
        reporter = AuditReporter(store_path=tmp_path / "audit.json")
        now = time.time()
        report = reporter.generate_report(now - 100, now)
        success = reporter.submit(report)
        assert success
        assert report.status == ReportStatus.SUBMITTED
        # 验证归档文件存在
        archive_dir = (tmp_path / "audit.json").parent / "submitted"
        archives = list(archive_dir.glob("*.json"))
        assert len(archives) == 1

    def test_submit_via_api_no_config_returns_false(self, tmp_path, monkeypatch):
        from deadman.compliance.audit_report import (
            AuditReporter,
            ReportStatus,
        )
        monkeypatch.setenv("DEADMAN_AUDIT_SUBMIT_CHANNEL", "api")
        # 不设置 API URL/TOKEN
        monkeypatch.delenv("DEADMAN_AUDIT_API_URL", raising=False)
        monkeypatch.delenv("DEADMAN_AUDIT_API_TOKEN", raising=False)
        reporter = AuditReporter(store_path=tmp_path / "audit.json")
        now = time.time()
        report = reporter.generate_report(now - 100, now)
        success = reporter.submit(report)
        assert not success
        assert report.status == ReportStatus.FAILED

    def test_acknowledge_updates_status(self, tmp_path):
        from deadman.compliance.audit_report import (
            AuditReporter,
            ReportStatus,
        )
        reporter = AuditReporter(store_path=tmp_path / "audit.json")
        now = time.time()
        report = reporter.generate_report(now - 100, now)
        reporter.submit(report)
        ok = reporter.acknowledge(report.report_id)
        assert ok
        assert report.status == ReportStatus.ACKNOWLEDGED
        assert report.acknowledged_at is not None

    def test_disabled_returns_archived(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.compliance.audit_report import AuditReporter, ReportStatus
        reporter = AuditReporter(store_path=tmp_path / "audit.json")
        report = reporter.generate_report(0, time.time())
        assert report.status == ReportStatus.ARCHIVED


# =====================================================================
# 5. retention
# =====================================================================

class TestRetention:
    def test_default_policy_audit_log_keep(self):
        from deadman.compliance.retention import RetentionManager, DataCategory, DisposalAction
        rm = RetentionManager()
        policy = rm.get_policy(DataCategory.AUDIT_LOG)
        assert policy.disposal_action == DisposalAction.KEEP
        assert policy.retention_days == 365 * 7

    def test_default_policy_billing_record_keep(self):
        from deadman.compliance.retention import RetentionManager, DataCategory, DisposalAction
        rm = RetentionManager()
        policy = rm.get_policy(DataCategory.BILLING_RECORD)
        assert policy.disposal_action == DisposalAction.KEEP
        assert policy.retention_days == 365 * 10

    def test_record_and_check_expiration(self, tmp_path):
        from deadman.compliance.retention import RetentionManager, DataCategory
        rm = RetentionManager(store_path=tmp_path / "retention.json")
        # 用户信息 7 年保留
        record = rm.record(
            DataCategory.USER_PROFILE,
            user_id="u1",
            data_id="profile_1",
            size_bytes=1024,
        )
        assert record.expires_at > record.created_at + 365 * 6 * 86400

    def test_run_sweep_cleans_expired(self, tmp_path):
        from deadman.compliance.retention import RetentionManager, DataCategory
        rm = RetentionManager(store_path=tmp_path / "retention.json")
        # 注册清理器
        cleaned = []
        rm.register_cleaner(
            DataCategory.TEMP_DATA,
            lambda uid, did: cleaned.append((uid, did)) or True,
        )
        # 手动插入已过期记录(7 天保留,8 天前创建)
        import time as _time
        from deadman.compliance.retention import RetentionRecord
        now = _time.time()
        record = RetentionRecord(
            category=DataCategory.TEMP_DATA,
            user_id="u1",
            tenant_id="default",
            data_id="temp_1",
            created_at=now - 8 * 86400,
            expires_at=now - 1 * 86400,  # 1 天前过期
        )
        rm._records[DataCategory.TEMP_DATA] = [record]
        rm._save()
        rm._loaded = True

        stats = rm.run_sweep(now=now)
        assert stats.get("temp_data", 0) == 1
        assert ("u1", "temp_1") in cleaned

    def test_run_sweep_skips_keep_category(self, tmp_path):
        from deadman.compliance.retention import RetentionManager, DataCategory, RetentionRecord
        import time as _time
        rm = RetentionManager(store_path=tmp_path / "retention.json")
        now = _time.time()
        # audit_log KEEP,即使过期也不清理
        record = RetentionRecord(
            category=DataCategory.AUDIT_LOG,
            user_id="u1",
            tenant_id="default",
            data_id="audit_1",
            created_at=now - 365 * 10 * 86400,
            expires_at=now - 1 * 86400,
        )
        rm._records[DataCategory.AUDIT_LOG] = [record]
        rm._save()
        rm._loaded = True

        stats = rm.run_sweep(now=now)
        # audit_log 不在 stats 中
        assert "audit_log" not in stats

    def test_tenant_override(self, tmp_path):
        from deadman.compliance.retention import RetentionManager, DataCategory
        rm = RetentionManager(store_path=tmp_path / "retention.json")
        # 默认 1 年
        default_policy = rm.get_policy(DataCategory.CHAT_HISTORY)
        assert default_policy.retention_days == 365
        # 租户覆盖为 30 天
        rm.set_tenant_override("t1", DataCategory.CHAT_HISTORY, retention_days=30)
        override = rm.get_policy(DataCategory.CHAT_HISTORY, tenant_id="t1")
        assert override.retention_days == 30

    def test_list_expiring(self, tmp_path):
        from deadman.compliance.retention import RetentionManager, DataCategory, RetentionRecord
        import time as _time
        rm = RetentionManager(store_path=tmp_path / "retention.json")
        now = _time.time()
        # 3 天后过期
        record = RetentionRecord(
            category=DataCategory.TEMP_DATA,
            user_id="u1",
            tenant_id="default",
            data_id="temp_1",
            created_at=now,
            expires_at=now + 3 * 86400,
        )
        rm._records[DataCategory.TEMP_DATA] = [record]
        rm._save()
        rm._loaded = True

        expiring = rm.list_expiring(within_days=7)
        assert len(expiring) == 1
        assert expiring[0].data_id == "temp_1"

    def test_disabled_record_returns_placeholder(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.compliance.retention import RetentionManager, DataCategory
        rm = RetentionManager(store_path=tmp_path / "retention.json")
        record = rm.record(DataCategory.CHAT_HISTORY, user_id="u1", data_id="msg_1")
        assert record.user_id == "u1"


# =====================================================================
# 6. consent
# =====================================================================

class TestConsent:
    def test_grant_and_check(self, tmp_path):
        from deadman.compliance.consent import ConsentManager, ConsentType, ConsentStatus
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        # 初始未同意
        assert not cm.check("u1", ConsentType.TERMS_OF_SERVICE)
        # 授予
        record = cm.grant("u1", ConsentType.TERMS_OF_SERVICE, source="web")
        assert record.status == ConsentStatus.GRANTED
        # 验证
        assert cm.check("u1", ConsentType.TERMS_OF_SERVICE)

    def test_withdraw(self, tmp_path):
        from deadman.compliance.consent import ConsentManager, ConsentType
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        cm.grant("u1", ConsentType.MARKETING, source="web")
        assert cm.check("u1", ConsentType.MARKETING)
        # 撤回
        cm.withdraw("u1", ConsentType.MARKETING, reason="no_more")
        assert not cm.check("u1", ConsentType.MARKETING)

    def test_version_mismatch_invalidates(self, tmp_path):
        from deadman.compliance.consent import ConsentManager, ConsentType
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        cm.grant("u1", ConsentType.PRIVACY_POLICY, source="web")
        assert cm.check("u1", ConsentType.PRIVACY_POLICY)
        # 更新版本
        affected = cm.update_consent_version(
            ConsentType.PRIVACY_POLICY,
            "2025.01.0",
        )
        assert affected == 1
        # 用户需重新同意
        assert not cm.check("u1", ConsentType.PRIVACY_POLICY)

    def test_history_preserved(self, tmp_path):
        from deadman.compliance.consent import ConsentManager, ConsentType, ConsentStatus
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        cm.grant("u1", ConsentType.MARKETING, source="web")
        cm.withdraw("u1", ConsentType.MARKETING, reason="done")
        history = cm.get_history("u1", ConsentType.MARKETING)
        assert len(history) == 2
        assert history[0].status == ConsentStatus.GRANTED
        assert history[1].status == ConsentStatus.WITHDRAWN

    def test_withdraw_without_grant_returns_none(self, tmp_path):
        from deadman.compliance.consent import ConsentManager, ConsentType
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        result = cm.withdraw("u1", ConsentType.MARKETING)
        assert result is None

    def test_export_for_audit(self, tmp_path):
        from deadman.compliance.consent import ConsentManager, ConsentType
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        cm.grant("u1", ConsentType.TERMS_OF_SERVICE, source="web")
        cm.grant("u2", ConsentType.TERMS_OF_SERVICE, source="api")
        records = cm.export_for_audit(consent_type=ConsentType.TERMS_OF_SERVICE)
        assert len(records) == 2
        # 按 user_id 过滤
        records_u1 = cm.export_for_audit(user_id="u1")
        assert len(records_u1) == 1

    def test_disabled_returns_granted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_COMPLIANCE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.compliance.consent import ConsentManager, ConsentType
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        # 关闭时默认 granted
        assert cm.check("u1", ConsentType.TERMS_OF_SERVICE)

    def test_list_user_consents(self, tmp_path):
        from deadman.compliance.consent import (
            ConsentManager,
            ConsentType,
            ConsentStatus,
        )
        cm = ConsentManager(store_path=tmp_path / "consents.json")
        cm.grant("u1", ConsentType.TERMS_OF_SERVICE)
        cm.grant("u1", ConsentType.PRIVACY_POLICY)
        cm.withdraw("u1", ConsentType.MARKETING)  # 未授予过 → 不写入
        consents = cm.list_user_consents("u1")
        assert "terms_of_service" in consents
        assert "privacy_policy" in consents
        assert consents["terms_of_service"] == ConsentStatus.GRANTED
        # marketing 没有 → 不在字典中
        assert "marketing" not in consents
