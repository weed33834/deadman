"""P8.1 商业化与计费测试 - 6 模块 + 40+ 测试用例。

覆盖:
    - plans: 计划定义 / 价格 / 限制 / 特性矩阵
    - subscription: 订阅 / 续费 / 退订 / 升降级 / 状态机
    - metering: 多维度计量 / 持久化 / 聚合查询
    - usage_tracker: 实时计数 / 配额检查 / 预测
    - invoice: 账单生成 / 发票导出 / 多支付网关 / 退款
    - cost_router: 多模型路由 / SLA / 成本 / 故障转移
"""

from __future__ import annotations

import json
import time

import pytest

# 注意:billing 模块依赖 infrastructure(feature_flags / quota / multi_tenant),
# 测试时需要 monkeypatch env var

# =====================================================================
# 公共 fixture
# =====================================================================


@pytest.fixture(autouse=True)
def enable_billing(monkeypatch):
    """每个测试都启用 billing(默认关闭)。

    billing 依赖 P7.7 quota 执行配额限制,所以也要启用 quota。
    """
    monkeypatch.setenv("DEADMAN_BILLING_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_QUOTA_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    # 清缓存
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    # 重置全局单例
    import deadman.billing as billing_pkg
    billing_pkg._sm_instance = None
    billing_pkg._ms_instance = None
    billing_pkg._ut_instance = None
    billing_pkg._ig_instance = None
    billing_pkg._cr_instance = None
    # 重置 quota 单例(避免上一个测试的状态泄漏)
    import deadman.infrastructure.quota as quota_pkg
    quota_pkg._qm_instance = None
    yield
    # 测试后重置(防污染其他测试)
    monkeypatch.setenv("DEADMAN_BILLING_ENABLED", "0")
    monkeypatch.setenv("DEADMAN_QUOTA_ENABLED", "0")
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    quota_pkg._qm_instance = None


# =====================================================================
# P8.1.1 plans 测试
# =====================================================================


class TestPlans:
    def test_three_plans_exist(self):
        from deadman.billing.plans import PLANS, PlanName
        assert PlanName.FREE.value in PLANS
        assert PlanName.PRO.value in PLANS
        assert PlanName.ENTERPRISE.value in PLANS

    def test_free_plan_price_zero(self):
        from deadman.billing.plans import FREE_PLAN
        assert FREE_PLAN.price_monthly == 0.0
        assert FREE_PLAN.price_yearly == 0.0

    def test_pro_plan_more_expensive_than_free(self):
        from deadman.billing.plans import FREE_PLAN, PRO_PLAN
        assert PRO_PLAN.price_monthly > FREE_PLAN.price_monthly

    def test_enterprise_plan_more_expensive_than_pro(self):
        from deadman.billing.plans import ENTERPRISE_PLAN, PRO_PLAN
        assert ENTERPRISE_PLAN.price_monthly > PRO_PLAN.price_monthly

    def test_yearly_discount(self):
        """年付应有折扣(月付*12 > 年付)。"""
        from deadman.billing.plans import PRO_PLAN
        monthly_total = PRO_PLAN.price_monthly * 12
        assert PRO_PLAN.price_yearly < monthly_total

    def test_enterprise_unlimited_tokens(self):
        """ENTERPRISE 无限 token(-1)。"""
        from deadman.billing.plans import ENTERPRISE_PLAN
        assert ENTERPRISE_PLAN.limits.llm_tokens_daily == -1
        assert ENTERPRISE_PLAN.limits.llm_tokens_monthly == -1

    def test_enterprise_more_features_than_pro(self):
        from deadman.billing.plans import ENTERPRISE_PLAN, PRO_PLAN
        assert len(ENTERPRISE_PLAN.features) > len(PRO_PLAN.features)

    def test_has_feature(self):
        from deadman.billing.plans import FREE_PLAN, PRO_PLAN
        assert FREE_PLAN.has_feature("debate") is True
        assert FREE_PLAN.has_feature("plan_execute") is False
        assert PRO_PLAN.has_feature("plan_execute") is True

    def test_data_retention_increasing(self):
        """数据保留期随 plan 递增。"""
        from deadman.billing.plans import ENTERPRISE_PLAN, FREE_PLAN, PRO_PLAN
        assert FREE_PLAN.data_retention_days < PRO_PLAN.data_retention_days
        assert PRO_PLAN.data_retention_days < ENTERPRISE_PLAN.data_retention_days

    def test_enterprise_7_year_retention(self):
        """ENTERPRISE 7 年保留(法规要求)。"""
        from deadman.billing.plans import ENTERPRISE_PLAN
        assert ENTERPRISE_PLAN.data_retention_days >= 2555  # 7 * 365

    def test_get_plan_unknown(self):
        from deadman.billing.plans import get_plan
        assert get_plan("nonexistent") is None

    def test_get_plan_known(self):
        from deadman.billing.plans import get_plan
        plan = get_plan("free")
        assert plan is not None
        assert plan.name.value == "free"

    def test_list_plans(self):
        from deadman.billing.plans import list_plans
        plans = list_plans()
        assert len(plans) == 3

    def test_sla_level_increasing(self):
        from deadman.billing.plans import ENTERPRISE_PLAN, FREE_PLAN, PRO_PLAN
        assert FREE_PLAN.sla_level == "none"
        assert PRO_PLAN.sla_level == "99"
        assert ENTERPRISE_PLAN.sla_level == "99.9"


# =====================================================================
# P8.1.2 subscription 测试
# =====================================================================


class TestSubscription:
    def test_subscribe_free(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sub = sm.subscribe("user1", "free")
        assert sub.user_id == "user1"
        assert sub.plan_name == "free"
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_subscribe_with_trial(self, tmp_path):
        from deadman.billing.subscription import SubscriptionStatus
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sub = sm.subscribe("user1", "pro", with_trial=True)
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.trial_end is not None

    def test_subscribe_enterprise_no_trial(self, tmp_path):
        """ENTERPRISE 不支持试用。"""
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sub = sm.subscribe("user1", "enterprise", with_trial=True)
        # with_trial 被 force False
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.trial_end is None

    def test_subscribe_unknown_plan_raises(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        with pytest.raises(ValueError):
            sm.subscribe("user1", "nonexistent_plan")

    def test_subscribe_duplicate_active_raises(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "free")
        with pytest.raises(ValueError):
            sm.subscribe("user1", "pro")

    def test_cancel_immediately(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sub = sm.cancel("user1", immediately=True, reason="too expensive")
        assert sub.status == SubscriptionStatus.CANCELED
        assert sub.canceled_at is not None

    def test_cancel_at_period_end(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sub = sm.cancel("user1", immediately=False)
        assert sub.status == SubscriptionStatus.ACTIVE  # 仍 ACTIVE
        assert sub.cancel_at_period_end is True

    def test_cancel_nonexistent_returns_none(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        assert sm.cancel("nonexistent") is None

    def test_upgrade_plan(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "free")
        sub = sm.upgrade("user1", "pro")
        assert sub.plan_name == "pro"
        # 历史记录
        assert len(sub.history) >= 1
        assert sub.history[-1]["to"] == "pro"

    def test_downgrade_plan(self, tmp_path):
        """upgrade() 也支持降级。"""
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sub = sm.upgrade("user1", "free", prorate=True)
        assert sub.plan_name == "free"
        # 历史标记为 downgrade
        assert sub.history[-1]["action"] == "downgrade"

    def test_upgrade_unknown_plan_raises(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "free")
        with pytest.raises(ValueError):
            sm.upgrade("user1", "nonexistent_plan")

    def test_upgrade_no_active_sub_raises(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        with pytest.raises(ValueError):
            sm.upgrade("user1", "pro")

    def test_renew_extends_period(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        old_sub = sm.get_current("user1")
        old_period_end = old_sub.current_period_end
        new_sub = sm.renew("user1")
        assert new_sub.current_period_end > old_period_end

    def test_get_effective_plan_no_sub(self, tmp_path):
        """无订阅 → free plan。"""
        from deadman.billing.plans import FREE_PLAN
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        plan = sm.get_effective_plan("nonexistent")
        assert plan.name == FREE_PLAN.name

    def test_get_effective_plan_with_sub(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        plan = sm.get_effective_plan("user1")
        assert plan.name.value == "pro"

    def test_advance_status_trial_to_active(self, tmp_path):
        """试用到期 → ACTIVE。"""
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro", with_trial=True)
        sub = sm.get_current("user1")
        # 把 trial_end 设为过去
        sub.trial_end = time.time() - 1
        sm._save()
        changed = sm.advance_status()
        assert changed == 1
        sub2 = sm.get_current("user1")
        assert sub2.status == SubscriptionStatus.ACTIVE

    def test_advance_status_active_to_past_due(self, tmp_path):
        """周期末未续费 → PAST_DUE。"""
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sub = sm.get_current("user1")
        # 把 period_end 设为过去
        sub.current_period_end = time.time() - 1
        sm._save()
        sm.advance_status()
        sub2 = sm.get_current("user1")
        assert sub2.status == SubscriptionStatus.PAST_DUE

    def test_advance_status_past_due_to_expired(self, tmp_path):
        """宽限期满 → EXPIRED。"""
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sub = sm.get_current("user1")
        sub.current_period_end = time.time() - 8 * 86400  # 8 天前过期(超 7 天宽限)
        sub.status = SubscriptionStatus.PAST_DUE
        sm._save()
        sm.advance_status()
        sub2 = sm.get_current("user1")
        assert sub2.status == SubscriptionStatus.EXPIRED

    def test_advance_status_cancel_at_period_end(self, tmp_path):
        """cancel_at_period_end=True 到期 → CANCELED。"""
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sm.cancel("user1", immediately=False)
        sub = sm.get_current("user1")
        sub.current_period_end = time.time() - 1
        sm._save()
        sm.advance_status()
        sub2 = sm.get_current("user1")
        assert sub2.status == SubscriptionStatus.CANCELED

    def test_persist_across_instances(self, tmp_path):
        """订阅信息跨实例持久化。"""
        from deadman.billing.subscription import SubscriptionManager
        store = tmp_path / "subs.json"
        sm1 = SubscriptionManager(store_path=store)
        sm1.subscribe("user1", "pro")
        sm2 = SubscriptionManager(store_path=store)
        sub = sm2.get_current("user1")
        assert sub is not None
        assert sub.plan_name == "pro"

    def test_is_active(self, tmp_path):
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        sub = sm.get_current("user1")
        assert sub.is_active() is True

        # 过期 → 不再 active
        sub.status = SubscriptionStatus.EXPIRED
        assert sub.is_active() is False


# =====================================================================
# P8.1.3 metering 测试
# =====================================================================


class TestMetering:
    def test_record_event(self, tmp_path):
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        event = ms.record_llm_tokens("user1", 1000, model="gpt-4o")
        assert event is not None
        assert event.amount == 1000
        assert event.user_id == "user1"
        assert event.model == "gpt-4o"

    def test_record_tool_call(self, tmp_path):
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        event = ms.record_tool_call("user1", tool_name="web_search")
        assert event is not None
        assert event.amount == 1
        assert event.tool_name == "web_search"

    def test_record_storage(self, tmp_path):
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        # 2.5 MB → 3 MB(向上取整)
        event = ms.record_storage("user1", 2 * 1024 * 1024 + 1)
        assert event is not None
        assert event.amount == 3

    def test_record_multimodal(self, tmp_path):
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        event = ms.record_multimodal("user1", "OCR")
        assert event is not None
        assert event.multimodal_type == "OCR"

    def test_negative_amount_ignored(self, tmp_path):
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        event = ms.record_llm_tokens("user1", -100)
        assert event is None

    def test_aggregate_single_day(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000)
        ms.record_llm_tokens("user1", 500)
        ms.record_tool_call("user1", tool_name="web_search")
        today = time.strftime("%Y-%m-%d", time.localtime())
        total = ms.aggregate("user1", MeteringDimension.LLM_TOKENS, today)
        assert total == 1500

    def test_aggregate_month(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000)
        ms.record_llm_tokens("user1", 500)
        month = time.strftime("%Y-%m", time.localtime())
        total = ms.aggregate("user1", MeteringDimension.LLM_TOKENS, month)
        assert total == 1500

    def test_aggregate_all(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000)
        total = ms.aggregate("user1", MeteringDimension.LLM_TOKENS, "all")
        assert total == 1000

    def test_aggregate_other_user(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000)
        ms.record_llm_tokens("user2", 500)
        total = ms.aggregate("user1", MeteringDimension.LLM_TOKENS, "all")
        assert total == 1000

    def test_get_daily_usage(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000, model="gpt-4o")
        ms.record_tool_call("user1", tool_name="web_search")
        today = time.strftime("%Y-%m-%d", time.localtime())
        usage = ms.get_daily_usage("user1", today)
        assert usage[MeteringDimension.LLM_TOKENS.value] == 1000
        assert usage[MeteringDimension.TOOL_CALLS.value] == 1

    def test_get_monthly_usage(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000)
        month = time.strftime("%Y-%m", time.localtime())
        usage = ms.get_monthly_usage("user1", month)
        assert usage[MeteringDimension.LLM_TOKENS.value] == 1000

    def test_no_data_returns_zero(self, tmp_path):
        from deadman.billing.metering import MeteringDimension, MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        total = ms.aggregate("nonexistent_user", MeteringDimension.LLM_TOKENS, "all")
        assert total == 0

    def test_file_per_day(self, tmp_path):
        """事件按天分文件。"""
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        ms.record_llm_tokens("user1", 1000)
        today = time.strftime("%Y-%m-%d", time.localtime())
        file_path = tmp_path / "metering" / f"metering_{today}.jsonl"
        assert file_path.exists()


# =====================================================================
# P8.1.4 usage_tracker 测试
# =====================================================================


class TestUsageTracker:
    def test_record_token(self, tmp_path):
        from deadman.billing.metering import MeteringService
        from deadman.billing.usage_tracker import UsageTracker
        from deadman.infrastructure.quota import QuotaManager
        from deadman.billing.subscription import SubscriptionManager

        ms = MeteringService(data_dir=tmp_path / "metering")
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        ut = UsageTracker(metering=ms, quota=qm, subscriptions=sm)

        result = ut.record_token("user1", 1000, model="gpt-4o")
        assert result.dimension == "llm_tokens"
        assert result.used == 1000

    def test_check_quota(self, tmp_path):
        from deadman.billing.metering import MeteringService
        from deadman.billing.usage_tracker import UsageTracker
        from deadman.infrastructure.quota import QuotaManager
        from deadman.billing.subscription import SubscriptionManager
        ms = MeteringService(data_dir=tmp_path / "metering")
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        ut = UsageTracker(metering=ms, quota=qm, subscriptions=sm)
        result = ut.check_quota("user1", "llm_tokens")
        assert result.dimension == "llm_tokens"
        assert result.limit > 0

    def test_get_usage_default_today(self, tmp_path):
        from deadman.billing.metering import MeteringService
        from deadman.billing.usage_tracker import UsageTracker
        from deadman.infrastructure.quota import QuotaManager
        from deadman.billing.subscription import SubscriptionManager
        ms = MeteringService(data_dir=tmp_path / "metering")
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        ut = UsageTracker(metering=ms, quota=qm, subscriptions=sm)
        ut.record_token("user1", 1000)
        report = ut.get_usage("user1")
        assert report.llm_tokens == 1000

    def test_predict_overflow_returns_none_when_under_limit(self, tmp_path):
        from deadman.billing.metering import MeteringService
        from deadman.billing.usage_tracker import UsageTracker
        from deadman.infrastructure.quota import QuotaManager
        from deadman.billing.subscription import SubscriptionManager
        ms = MeteringService(data_dir=tmp_path / "metering")
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        ut = UsageTracker(metering=ms, quota=qm, subscriptions=sm)
        # 用量很少,不应预测超限
        assert ut.predict_overflow("user1", "llm_tokens") is None

    def test_quota_rejection_handled(self, tmp_path):
        """配额拒绝:不抛异常,返回 will_exceed=True。"""
        from deadman.billing.metering import MeteringService
        from deadman.billing.usage_tracker import UsageTracker
        from deadman.infrastructure.quota import QuotaManager
        from deadman.billing.subscription import SubscriptionManager
        ms = MeteringService(data_dir=tmp_path / "metering")
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        qm = QuotaManager(store_path=tmp_path / "quota.json")
        ut = UsageTracker(metering=ms, quota=qm, subscriptions=sm)
        # 一次消费超 limit
        result = ut.record_token("user1", 1_000_000_000)  # 远超 limit
        assert result.will_exceed is True


# =====================================================================
# P8.1.5 invoice 测试
# =====================================================================


class TestInvoice:
    def _setup_with_sub(self, tmp_path, plan_name="pro"):
        from deadman.billing.invoice import InvoiceGenerator
        from deadman.billing.metering import MeteringService
        from deadman.billing.subscription import SubscriptionManager
        ms = MeteringService(data_dir=tmp_path / "metering")
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", plan_name)
        ig = InvoiceGenerator(
            store_path=tmp_path / "invoices.json",
            subscriptions=sm,
            metering=ms,
        )
        return ig, sm, ms

    def test_generate_invoice(self, tmp_path):
        ig, sm, ms = self._setup_with_sub(tmp_path, "pro")
        now = time.time()
        period_start = now - 30 * 86400
        period_end = now
        invoice = ig.generate("user1", period_start, period_end)
        assert invoice is not None
        assert invoice.user_id == "user1"
        assert invoice.plan_name == "pro"
        assert invoice.total > 0  # pro plan 有基础费
        # 至少有 1 行(plan 基础费)
        assert len(invoice.line_items) >= 1

    def test_generate_free_plan_zero_total(self, tmp_path):
        ig, sm, ms = self._setup_with_sub(tmp_path, "free")
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        assert invoice is not None
        assert invoice.total == 0  # free plan 免费

    def test_generate_with_overage(self, tmp_path):
        """超量计费。"""
        ig, sm, ms = self._setup_with_sub(tmp_path, "pro")
        # 模拟超量(超过 PRO plan 月度 token 限额 10_000_000)
        ms.record_llm_tokens("user1", 11_000_000, model="gpt-4o")
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        # 应有 plan 费 + 超量费
        assert any(li.overage for li in invoice.line_items)
        assert invoice.total > 99  # 99 + 超量费

    def test_export_json(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        data = ig.export(invoice.invoice_id, format="json")
        assert data is not None
        parsed = json.loads(data)
        assert parsed["invoice_id"] == invoice.invoice_id

    def test_export_csv(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        data = ig.export(invoice.invoice_id, format="csv")
        assert data is not None
        text = data.decode("utf-8")
        assert "description" in text
        assert "总计" in text

    def test_export_html(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        data = ig.export(invoice.invoice_id, format="html")
        assert data is not None
        text = data.decode("utf-8")
        assert "<html" in text
        assert invoice.invoice_id in text

    def test_export_unknown_invoice(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        assert ig.export("nonexistent", "json") is None

    def test_export_unknown_format(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        assert ig.export(invoice.invoice_id, "unknown") is None

    def test_send_to_payment_gateway(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        payment_id = ig.send_to_payment_gateway(invoice.invoice_id, "stripe")
        assert payment_id is not None
        assert payment_id.startswith("stripe_")
        # invoice 状态变 OPEN
        inv2 = ig.get(invoice.invoice_id)
        from deadman.billing.invoice import InvoiceStatus
        assert inv2.status == InvoiceStatus.OPEN

    def test_unknown_payment_gateway_raises(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        with pytest.raises(ValueError):
            ig.send_to_payment_gateway(invoice.invoice_id, "unknown_gateway")

    def test_mark_paid(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        ig.send_to_payment_gateway(invoice.invoice_id, "stripe")
        ig.mark_paid(invoice.invoice_id, "stripe_123")
        inv2 = ig.get(invoice.invoice_id)
        from deadman.billing.invoice import InvoiceStatus
        assert inv2.status == InvoiceStatus.PAID
        assert inv2.paid_at is not None

    def test_refund_full(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        ig.send_to_payment_gateway(invoice.invoice_id, "stripe")
        ig.mark_paid(invoice.invoice_id, "stripe_123")
        ig.refund(invoice.invoice_id, reason="user requested")
        inv2 = ig.get(invoice.invoice_id)
        from deadman.billing.invoice import InvoiceStatus
        assert inv2.status == InvoiceStatus.REFUNDED
        assert inv2.refunded_amount == inv2.total

    def test_refund_partial(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        ig.send_to_payment_gateway(invoice.invoice_id, "stripe")
        ig.mark_paid(invoice.invoice_id, "stripe_123")
        ig.refund(invoice.invoice_id, amount=50.0, reason="partial refund")
        inv2 = ig.get(invoice.invoice_id)
        # 部分退款不改变状态(仍 PAID)
        from deadman.billing.invoice import InvoiceStatus
        assert inv2.status == InvoiceStatus.PAID
        assert inv2.refunded_amount == 50.0

    def test_void_invoice(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        ig.void(invoice.invoice_id, reason="duplicate")
        inv2 = ig.get(invoice.invoice_id)
        from deadman.billing.invoice import InvoiceStatus
        assert inv2.status == InvoiceStatus.VOID

    def test_void_paid_invoice_raises(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        ig.send_to_payment_gateway(invoice.invoice_id, "stripe")
        ig.mark_paid(invoice.invoice_id, "stripe_123")
        with pytest.raises(ValueError):
            ig.void(invoice.invoice_id)

    def test_list_by_user(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        ig.generate("user1", now - 60 * 86400, now - 30 * 86400)
        ig.generate("user1", now - 30 * 86400, now)
        invoices = ig.list_by_user("user1")
        assert len(invoices) == 2

    def test_list_by_status(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        ig.generate("user1", now - 30 * 86400, now)
        from deadman.billing.invoice import InvoiceStatus
        drafts = ig.list_by_status(InvoiceStatus.DRAFT)
        assert len(drafts) == 1

    def test_persist_across_instances(self, tmp_path):
        ig, _, _ = self._setup_with_sub(tmp_path)
        now = time.time()
        invoice = ig.generate("user1", now - 30 * 86400, now)
        ig2 = ig.__class__(
            store_path=ig.store_path,
            subscriptions=ig.subscriptions,
            metering=ig.metering,
        )
        inv2 = ig2.get(invoice.invoice_id)
        assert inv2 is not None
        assert inv2.invoice_id == invoice.invoice_id


# =====================================================================
# P8.1.6 cost_router 测试
# =====================================================================


class TestCostRouter:
    def test_route_free_plan_cost_first(self, tmp_path):
        from deadman.billing.cost_router import CostRouter
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "free")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", task_complexity="small")
        assert result.chosen is not None
        # free 限制在 SMALL tier
        from deadman.billing.cost_router import ModelTier
        assert cr._tier_rank(result.chosen.tier) <= cr._tier_rank(ModelTier.SMALL)

    def test_route_pro_plan_quality_first(self, tmp_path):
        from deadman.billing.cost_router import CostRouter, ModelTier
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", task_complexity="medium")
        assert result.chosen is not None
        # pro 限制在 LARGE tier
        assert cr._tier_rank(result.chosen.tier) <= cr._tier_rank(ModelTier.LARGE)

    def test_route_enterprise_can_use_reasoning(self, tmp_path):
        from deadman.billing.cost_router import CostRouter, ModelTier
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "enterprise")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", task_complexity="reasoning")
        assert result.chosen is not None
        # enterprise 可用 REASONING
        assert result.chosen.tier == ModelTier.REASONING

    def test_route_filters_by_tool_support(self, tmp_path):
        from deadman.billing.cost_router import CostRouter
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", requires_tools=True)
        assert result.chosen is not None
        assert result.chosen.supports_tools is True

    def test_route_filters_by_vision_support(self, tmp_path):
        from deadman.billing.cost_router import CostRouter
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", requires_vision=True)
        assert result.chosen is not None
        assert result.chosen.supports_vision is True

    def test_route_returns_alternatives(self, tmp_path):
        from deadman.billing.cost_router import CostRouter
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", task_complexity="medium")
        assert result.chosen is not None
        # 备选应 ≤ 2 个
        assert len(result.alternatives) <= 2

    def test_route_estimates_cost(self, tmp_path):
        from deadman.billing.cost_router import CostRouter
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", task_complexity="medium")
        assert result.estimated_cost > 0
        assert result.estimated_latency_ms > 0

    def test_get_failover(self, tmp_path):
        from deadman.billing.cost_router import CostRouter, ModelChoice, ModelTier
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        # 原模型 openai/gpt-4o
        original = ModelChoice(
            "openai", "gpt-4o", ModelTier.MEDIUM, 0.85, 0.03,
            supports_tools=True, supports_json_mode=True, max_context=128000,
        )
        failover = cr.get_failover(original)
        # 应返回非 openai/gpt-4o 的候选
        assert failover is not None
        assert not (failover.provider == original.provider and failover.model == original.model)

    def test_register_model(self, tmp_path):
        from deadman.billing.cost_router import CostRouter, ModelChoice, ModelTier
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        cr = CostRouter(subscriptions=sm)
        new_model = ModelChoice(
            "custom_provider", "custom-model", ModelTier.MEDIUM, 0.7, 0.002,
            supports_tools=True, max_context=64000,
        )
        cr.register_model(ModelTier.MEDIUM, new_model)
        # 注册后能在列表找到
        models = cr.list_models(ModelTier.MEDIUM)
        providers = [m.provider for m in models]
        assert "custom_provider" in providers

    def test_list_models_all_tiers(self, tmp_path):
        from deadman.billing.cost_router import CostRouter
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        cr = CostRouter(subscriptions=sm)
        all_models = cr.list_models()
        # 应该有 ≥ 5 个模型(每 tier 至少 1 个)
        assert len(all_models) >= 5

    def test_list_models_by_tier(self, tmp_path):
        from deadman.billing.cost_router import CostRouter, ModelTier
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        cr = CostRouter(subscriptions=sm)
        models = cr.list_models(ModelTier.MEDIUM)
        for m in models:
            assert m.tier == ModelTier.MEDIUM

    def test_strategy_cost_first_prefers_cheap(self, tmp_path):
        from deadman.billing.cost_router import CostRouter, RoutingStrategy
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sm.subscribe("user1", "pro")
        cr = CostRouter(subscriptions=sm)
        result = cr.route("user1", task_complexity="medium", strategy=RoutingStrategy.COST_FIRST.value)
        # 应该是最便宜的
        alternatives = cr.list_models()
        medium_models = [m for m in alternatives if m.tier.value == "medium"]
        if medium_models:
            cheapest = min(medium_models, key=lambda m: m.price_per_1k)
            assert result.chosen.price_per_1k <= cheapest.price_per_1k + 0.001  # 容差


# =====================================================================
# billing disabled 测试(向后兼容)
# =====================================================================


class TestBillingDisabled:
    """billing 关闭时所有功能透传,不报错。"""

    def test_subscribe_returns_free(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_BILLING_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.billing.subscription import SubscriptionManager, SubscriptionStatus
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        sub = sm.subscribe("user1", "pro")  # 即使请求 pro,关闭时返回 free
        assert sub.plan_name == "free"
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_metering_record_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_BILLING_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.billing.metering import MeteringService
        ms = MeteringService(data_dir=tmp_path / "metering")
        assert ms.record_llm_tokens("user1", 1000) is None

    def test_get_effective_plan_returns_free(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_BILLING_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.billing.plans import FREE_PLAN
        from deadman.billing.subscription import SubscriptionManager
        sm = SubscriptionManager(store_path=tmp_path / "subs.json")
        plan = sm.get_effective_plan("user1")
        assert plan.name == FREE_PLAN.name
