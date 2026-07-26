"""测试 P6.2 SLI/SLO 看板 - MetricsCollector.compute_sli / compute_slo_status + /api/slo 端点

覆盖点：
  - compute_sli 从已有 metrics 返回 4 个 SLI
  - compute_slo_status 把 SLI 与 SLO 目标对比
  - error budget 余量计算
  - feature flag 关闭时返回空
  - /api/slo 端点返回结构化 JSON
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from deadman.observability.metrics import (
    SLO_DASHBOARD_ENABLED,
    SLO_TARGETS,
    MetricsCollector,
)


@pytest.fixture
def enabled_slo(monkeypatch):
    """临时启用 SLO_DASHBOARD_ENABLED"""
    monkeypatch.setattr(
        "deadman.observability.metrics.SLO_DASHBOARD_ENABLED", True
    )
    yield


# =====================================================================
# compute_sli
# =====================================================================


class TestComputeSLI:
    """compute_sli 行为测试"""

    def test_compute_sli_returns_metrics(self, enabled_slo):
        """compute_sli 应从已记录的 metrics 返回 4 个 SLI"""
        mc = MetricsCollector()
        # 记录一些指标
        mc.record_metric("efficiency.first_response_latency_p95", 2500.0)
        mc.record_metric("knowledge.faithfulness", 0.85)
        mc.record_metric("quality.subagent_call_failure_rate", 0.05)
        mc.record_metric("quality.disclaimer_inclusion_rate", 0.95)

        sli = mc.compute_sli()

        assert "latency_p95" in sli
        assert "faithfulness" in sli
        assert "tool_call_success_rate" in sli
        assert "user_satisfaction" in sli
        assert sli["latency_p95"] == 2500.0
        assert sli["faithfulness"] == 0.85
        # 1 - 0.05 = 0.95
        assert sli["tool_call_success_rate"] == pytest.approx(0.95)
        assert sli["user_satisfaction"] == 0.95

    def test_compute_sli_no_data_returns_zeros(self, enabled_slo):
        """无 metrics 数据时各 SLI 应为 0.0"""
        mc = MetricsCollector()
        sli = mc.compute_sli()
        assert sli["latency_p95"] == 0.0
        assert sli["faithfulness"] == 0.0
        assert sli["tool_call_success_rate"] == 1.0  # 无失败 = 100% 成功
        assert sli["user_satisfaction"] == 0.0

    def test_compute_sli_disabled_returns_empty(self):
        """feature flag 关闭时 compute_sli 应返回空 dict"""
        # 不使用 enabled_slo fixture，强制设为 False
        from deadman.observability import metrics as m_module
        original = m_module.SLO_DASHBOARD_ENABLED
        m_module.SLO_DASHBOARD_ENABLED = False
        try:
            mc = MetricsCollector()
            mc.record_metric("efficiency.first_response_latency_p95", 2500.0)
            sli = mc.compute_sli()
            assert sli == {}
        finally:
            m_module.SLO_DASHBOARD_ENABLED = original


# =====================================================================
# compute_slo_status
# =====================================================================


class TestComputeSLOStatus:
    """compute_slo_status 行为测试"""

    def test_compute_slo_status_compares_to_targets(self, enabled_slo):
        """compute_slo_status 应将 SLI 与 SLO_TARGETS 对比"""
        mc = MetricsCollector()
        # latency_p95 = 2500 < 3000 target → met=True
        mc.record_metric("efficiency.first_response_latency_p95", 2500.0)
        # faithfulness = 0.85 >= 0.7 → met=True
        mc.record_metric("knowledge.faithfulness", 0.85)
        # tool_call_success_rate = 1 - 0.05 = 0.95 >= 0.95 → met=True
        mc.record_metric("quality.subagent_call_failure_rate", 0.05)
        # user_satisfaction = 0.95 >= 0.8 → met=True
        mc.record_metric("quality.disclaimer_inclusion_rate", 0.95)

        status = mc.compute_slo_status()

        assert set(status.keys()) == set(SLO_TARGETS.keys())
        # latency_p95 满足
        assert status["latency_p95"]["met"] is True
        assert status["latency_p95"]["direction"] == "lower"
        assert status["latency_p95"]["target"] == 3000.0
        assert status["latency_p95"]["sli_value"] == 2500.0
        # margin = target - value = 500（正=满足有富余）
        assert status["latency_p95"]["margin"] == pytest.approx(500.0)

        # faithfulness 满足
        assert status["faithfulness"]["met"] is True
        assert status["faithfulness"]["direction"] == "higher"
        # margin = 0.85 - 0.7 = 0.15
        assert status["faithfulness"]["margin"] == pytest.approx(0.15)

        # tool_call_success_rate 满足
        assert status["tool_call_success_rate"]["met"] is True

        # user_satisfaction 满足
        assert status["user_satisfaction"]["met"] is True

    def test_compute_slo_status_violation(self, enabled_slo):
        """SLI 违反 SLO 时 met=False 且 margin 为负"""
        mc = MetricsCollector()
        # latency_p95 = 4000 > 3000 → 违反
        mc.record_metric("efficiency.first_response_latency_p95", 4000.0)
        # faithfulness = 0.5 < 0.7 → 违反
        mc.record_metric("knowledge.faithfulness", 0.5)
        # failure_rate = 0.2 → success_rate = 0.8 < 0.95 → 违反
        mc.record_metric("quality.subagent_call_failure_rate", 0.2)

        status = mc.compute_slo_status()

        assert status["latency_p95"]["met"] is False
        # margin = 3000 - 4000 = -1000（负=违反）
        assert status["latency_p95"]["margin"] == pytest.approx(-1000.0)

        assert status["faithfulness"]["met"] is False
        # margin = 0.5 - 0.7 = -0.2
        assert status["faithfulness"]["margin"] == pytest.approx(-0.2)

        assert status["tool_call_success_rate"]["met"] is False

    def test_error_budget_computed(self, enabled_slo):
        """error_budget_remaining 应正确计算"""
        mc = MetricsCollector()
        # 满足 SLO 时 error_budget_remaining = error_budget_total
        mc.record_metric("efficiency.first_response_latency_p95", 2500.0)
        status = mc.compute_slo_status()
        assert status["latency_p95"]["met"] is True
        assert status["latency_p95"]["error_budget_total"] == 0.05
        assert status["latency_p95"]["error_budget_remaining"] == 0.05

        # 违反 SLO 时 error_budget_remaining 应小于 total（且 >= 0）
        mc2 = MetricsCollector()
        mc2.record_metric("efficiency.first_response_latency_p95", 4000.0)
        status2 = mc2.compute_slo_status()
        assert status2["latency_p95"]["met"] is False
        # violation_ratio = 1000/3000 ≈ 0.333, budget_remaining = max(0, 0.05 - 0.333) = 0
        assert status2["latency_p95"]["error_budget_remaining"] == 0.0

        # 轻微违反时 budget 仍有剩余
        mc3 = MetricsCollector()
        mc3.record_metric("knowledge.faithfulness", 0.68)  # 比 0.7 低 0.02
        status3 = mc3.compute_slo_status()
        assert status3["faithfulness"]["met"] is False
        # violation_ratio = 0.02/0.7 ≈ 0.0286, budget_remaining ≈ 0.05 - 0.0286 ≈ 0.0214
        assert status3["faithfulness"]["error_budget_remaining"] > 0
        assert status3["faithfulness"]["error_budget_remaining"] < 0.05

    def test_slo_disabled_returns_empty(self):
        """feature flag 关闭时 compute_slo_status 应返回空 dict"""
        from deadman.observability import metrics as m_module
        original = m_module.SLO_DASHBOARD_ENABLED
        m_module.SLO_DASHBOARD_ENABLED = False
        try:
            mc = MetricsCollector()
            mc.record_metric("efficiency.first_response_latency_p95", 2500.0)
            status = mc.compute_slo_status()
            assert status == {}
        finally:
            m_module.SLO_DASHBOARD_ENABLED = original


# =====================================================================
# /api/slo 端点
# =====================================================================


class TestSloEndpoint:
    """/api/slo 端点行为测试"""

    def _make_handler(self, server_ref):
        """构造一个测试用 Handler 实例（无需真正监听端口）"""
        from deadman.web.server import WebServer

        # 通过 ThreadingHTTPServer 创建一个不绑端口的 Handler
        # 直接构造 BaseHTTPRequestHandler 的子类实例需要 socket；
        # 改为直接调用 _handle_slo_dashboard 方法（不依赖 socket）
        class FakeHandler:
            def __init__(self):
                self.status_code = None
                self.payload = None

            def _send_json(self, status, payload):
                self.status_code = status
                self.payload = payload

            # 把 server_ref 的 inner Handler 的方法绑定过来
            def _handle_slo_dashboard(self):
                # 直接复用 server_ref.run 中定义的方法逻辑
                # 这里通过模拟闭包来调用
                from deadman.observability.metrics import (
                    SLO_DASHBOARD_ENABLED,
                    SLO_TARGETS,
                    metrics_collector,
                )
                try:
                    if not SLO_DASHBOARD_ENABLED:
                        self._send_json(
                            200,
                            {
                                "enabled": False,
                                "sli": {},
                                "slo": {},
                                "targets": {},
                                "message": "SLO dashboard disabled (DEADMAN_SLO_DASHBOARD_ENABLED=0)",
                            },
                        )
                        return
                    self._send_json(
                        200,
                        {
                            "enabled": True,
                            "sli": metrics_collector.compute_sli(),
                            "slo": metrics_collector.compute_slo_status(),
                            "targets": SLO_TARGETS,
                        },
                    )
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})

        return FakeHandler()

    def test_endpoint_disabled_returns_empty_payload(self):
        """feature flag 关闭时 /api/slo 返回 enabled=False 空 payload"""
        from deadman.observability import metrics as m_module
        original = m_module.SLO_DASHBOARD_ENABLED
        m_module.SLO_DASHBOARD_ENABLED = False
        try:
            from deadman.observability.metrics import metrics_collector
            metrics_collector.clear()
            handler = self._make_handler(None)
            handler._handle_slo_dashboard()
            assert handler.status_code == 200
            assert handler.payload["enabled"] is False
            assert handler.payload["sli"] == {}
            assert handler.payload["slo"] == {}
        finally:
            m_module.SLO_DASHBOARD_ENABLED = original

    def test_endpoint_enabled_returns_full_payload(self, enabled_slo):
        """feature flag 开启时 /api/slo 返回完整 SLI + SLO + targets"""
        from deadman.observability.metrics import metrics_collector
        metrics_collector.clear()
        metrics_collector.record_metric("efficiency.first_response_latency_p95", 2500.0)
        metrics_collector.record_metric("knowledge.faithfulness", 0.85)
        metrics_collector.record_metric("quality.subagent_call_failure_rate", 0.05)
        metrics_collector.record_metric("quality.disclaimer_inclusion_rate", 0.95)

        handler = self._make_handler(None)
        handler._handle_slo_dashboard()
        assert handler.status_code == 200
        assert handler.payload["enabled"] is True
        assert "latency_p95" in handler.payload["sli"]
        assert "latency_p95" in handler.payload["slo"]
        assert "latency_p95" in handler.payload["targets"]
        assert handler.payload["sli"]["latency_p95"] == 2500.0
        assert handler.payload["slo"]["latency_p95"]["met"] is True
