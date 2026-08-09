"""测试 deadman.observability - Tracer 与 MetricsCollector

覆盖点：
  - Tracer start_span / end_span 生命周期
  - Tracer get_span / get_trace / get_spans 查询
  - MetricsCollector record_metric / get_metric 记录与查询
  - SpanType 11 种类型
"""

from __future__ import annotations

import pytest
from deadman.observability.metrics import (
    METRIC_CATEGORIES,
    MetricsCollector,
    metrics_collector,
)
from deadman.observability.tracer import SpanType, Tracer, tracer

# =====================================================================
# SpanType - 11 种 span 类型
# =====================================================================


class TestSpanType:
    """测试 SpanType 枚举 - 11 种 span 类型"""

    def test_span_type_count(self):
        # 恰好 11 种
        assert len(list(SpanType)) == 11

    def test_span_type_values(self):
        # 关键类型齐全
        assert SpanType.ROOT.value == "root"
        assert SpanType.AGENT.value == "agent"
        assert SpanType.TOOL.value == "tool"
        assert SpanType.TRANSFER.value == "transfer"
        assert SpanType.REFLEXION.value == "reflexion"
        assert SpanType.LLM_JUDGE.value == "llm_judge"

    def test_all_expected_types(self):
        expected = {
            "root",
            "agent",
            "subagent",
            "transfer",
            "rule",
            "tool",
            "debate",
            "memory",
            "a2a",
            "reflexion",
            "llm_judge",
        }
        actual = {s.value for s in SpanType}
        assert actual == expected


# =====================================================================
# Tracer - start_span / end_span
# =====================================================================


class TestTracerSpan:
    """测试 Tracer span 生命周期"""

    def test_start_span_returns_id(self):
        # start_span 返回 span_id（字符串）
        t = Tracer()
        span_id = t.start_span(SpanType.ROOT, "test.span")
        assert isinstance(span_id, str)
        assert len(span_id) > 0

    def test_start_span_stores_attributes(self):
        # attributes 应被存储
        t = Tracer()
        span_id = t.start_span(SpanType.TOOL, "tool.test", {"tool_name": "web_search"})
        span = t.get_span(span_id)
        assert span is not None
        assert span["attributes"]["tool_name"] == "web_search"
        assert span["name"] == "tool.test"
        assert span["span_type"] == "tool"

    def test_end_span_writes_status_and_end_time(self):
        # end_span 应写入 status 和 end_time
        t = Tracer()
        span_id = t.start_span(SpanType.AGENT, "agent.test")
        # 初始 end_time 为 None
        assert t.get_span(span_id)["end_time"] is None
        t.end_span(span_id, status="OK")
        span = t.get_span(span_id)
        assert span["end_time"] is not None
        assert span["status"] == "OK"

    def test_end_span_with_events(self):
        # end_span 应追加 events
        t = Tracer()
        span_id = t.start_span(SpanType.TOOL, "tool.test")
        t.end_span(span_id, status="ERROR", events=[{"name": "exception"}])
        span = t.get_span(span_id)
        assert len(span["events"]) == 1
        assert span["events"][0]["name"] == "exception"
        assert span["status"] == "ERROR"

    def test_end_span_unknown_id_noop(self):
        # 结束不存在的 span_id → 无副作用
        t = Tracer()
        t.end_span("non-existent-id")  # 不应抛异常

    def test_current_span_id_tracks_stack(self):
        # current_span_id 跟踪栈顶
        t = Tracer()
        assert t.current_span_id is None
        s1 = t.start_span(SpanType.ROOT, "root")
        assert t.current_span_id == s1
        s2 = t.start_span(
            SpanType.AGENT,
            "agent",
        )  # 嵌套
        assert t.current_span_id == s2
        t.end_span(s2)
        assert t.current_span_id == s1
        t.end_span(s1)
        assert t.current_span_id is None

    def test_parent_span_id_set(self):
        # 嵌套 span 应设置 parent_span_id
        t = Tracer()
        parent = t.start_span(SpanType.ROOT, "parent")
        child = t.start_span(SpanType.AGENT, "child")
        child_span = t.get_span(child)
        assert child_span["parent_span_id"] == parent
        # 同一 trace_id
        assert child_span["trace_id"] == t.get_span(parent)["trace_id"]
        t.end_span(child)
        t.end_span(parent)


# =====================================================================
# Tracer - 查询 API
# =====================================================================


class TestTracerQuery:
    """测试 Tracer 查询 API"""

    def test_get_span_returns_none_for_unknown(self):
        t = Tracer()
        assert t.get_span("unknown") is None

    def test_get_spans_returns_all(self):
        # get_spans 返回所有 span
        t = Tracer()
        s1 = t.start_span(SpanType.ROOT, "a")
        s2 = t.start_span(SpanType.AGENT, "b")
        spans = t.get_spans()
        assert len(spans) == 2
        t.end_span(s2)
        t.end_span(s1)

    def test_get_trace_returns_by_trace_id(self):
        # get_trace 按 trace_id 取 span
        t = Tracer()
        s1 = t.start_span(SpanType.ROOT, "root")
        trace_id = t.get_span(s1)["trace_id"]
        s2 = t.start_span(SpanType.AGENT, "agent")
        # 同一 trace
        spans = t.get_trace(trace_id)
        assert len(spans) == 2
        t.end_span(s2)
        t.end_span(s1)

    def test_clear_resets_all(self):
        # clear 清空所有 span
        t = Tracer()
        t.start_span(SpanType.ROOT, "x")
        t.clear()
        assert t.get_spans() == []
        assert t.current_span_id is None


# =====================================================================
# Tracer - 全局单例
# =====================================================================


class TestTracerSingleton:
    """测试全局 tracer 单例"""

    def test_tracer_singleton_exists(self):
        assert tracer is not None
        assert isinstance(tracer, Tracer)

    def test_tracer_clear_in_fixture(self):
        # conftest 的 autouse fixture 应已清空 tracer
        assert tracer.get_spans() == []


# =====================================================================
# trace_tool_span / trace_root_span - 上下文管理器
# =====================================================================


class TestTraceContextManagers:
    """测试 trace_tool_span / trace_root_span 上下文管理器"""

    def test_trace_tool_span_creates_and_ends(self):
        # with 块进入时创建 span，退出时结束
        from deadman.observability.tracer import trace_tool_span

        with trace_tool_span("web_search", {"query": "x"}) as span_id:
            assert span_id is not None
            span = tracer.get_span(span_id)
            assert span is not None
            assert span["end_time"] is None  # 还未结束
        # 退出后已结束
        span = tracer.get_span(span_id)
        assert span["end_time"] is not None
        assert span["status"] == "OK"

    def test_trace_tool_span_error_status(self):
        # with 块内抛异常 → status=ERROR
        from deadman.observability.tracer import trace_tool_span

        with pytest.raises(ValueError), trace_tool_span("bad_tool") as span_id:
            raise ValueError("boom")
        span = tracer.get_span(span_id)
        assert span["status"] == "ERROR"
        assert len(span["events"]) >= 1

    def test_trace_root_span(self):
        from deadman.observability.tracer import trace_root_span

        with trace_root_span("user_request", {"user": "u1"}) as span_id:
            span = tracer.get_span(span_id)
            assert span["span_type"] == "root"
        span = tracer.get_span(span_id)
        assert span["status"] == "OK"


# =====================================================================
# MetricsCollector - record / get
# =====================================================================


class TestMetricsCollector:
    """测试 MetricsCollector 指标记录与查询"""

    def test_record_and_get_single(self):
        # 记录单个指标并查询
        mc = MetricsCollector()
        mc.record_metric("quality.rule_violation_rate", 0.1)
        stats = mc.get_metric("quality.rule_violation_rate")
        assert stats["count"] == 1
        assert stats["avg"] == 0.1
        assert stats["last"] == 0.1

    def test_record_multiple_aggregates(self):
        # 多次记录聚合
        mc = MetricsCollector()
        mc.record_metric("quality.rule_violation_rate", 0.1)
        mc.record_metric("quality.rule_violation_rate", 0.3)
        mc.record_metric("quality.rule_violation_rate", 0.5)
        stats = mc.get_metric("quality.rule_violation_rate")
        assert stats["count"] == 3
        assert stats["sum"] == pytest.approx(0.9)
        assert stats["avg"] == pytest.approx(0.3)
        assert stats["min"] == 0.1
        assert stats["max"] == 0.5
        assert stats["last"] == 0.5

    def test_record_with_tags(self):
        # 带 tags 的指标分组聚合
        mc = MetricsCollector()
        mc.record_metric("quality.rule_violation_rate", 0.1, tags={"platform": "trae"})
        mc.record_metric("quality.rule_violation_rate", 0.2, tags={"platform": "openai"})
        mc.record_metric("quality.rule_violation_rate", 0.3, tags={"platform": "trae"})
        # 只查 trae 平台
        trae_stats = mc.get_metric("quality.rule_violation_rate", tags={"platform": "trae"})
        assert trae_stats["count"] == 2
        assert trae_stats["avg"] == pytest.approx(0.2)
        # 不带 tags 查全部
        all_stats = mc.get_metric("quality.rule_violation_rate")
        assert all_stats["count"] == 3

    def test_record_bool_converts_to_float(self):
        # 布尔值转为 0.0/1.0
        mc = MetricsCollector()
        mc.record_metric("quality.ai_identity_disclosed", True)
        mc.record_metric("quality.ai_identity_disclosed", False)
        stats = mc.get_metric("quality.ai_identity_disclosed")
        assert stats["count"] == 2
        assert stats["sum"] == pytest.approx(1.0)

    def test_get_metric_unknown_returns_empty(self):
        # 未记录的指标返回空统计
        mc = MetricsCollector()
        stats = mc.get_metric("nonexistent.metric")
        assert stats["count"] == 0
        assert stats["avg"] == 0.0

    def test_clear_resets(self):
        # clear 清空所有指标
        mc = MetricsCollector()
        mc.record_metric("quality.x", 1.0)
        mc.clear()
        assert mc.get_metric("quality.x")["count"] == 0

    def test_list_metrics(self):
        # list_metrics 返回已记录的指标名
        mc = MetricsCollector()
        mc.record_metric("quality.rule_violation_rate", 0.1)
        mc.record_metric("efficiency.first_response_latency_p50", 100.0)
        names = mc.list_metrics()
        assert "quality.rule_violation_rate" in names
        assert "efficiency.first_response_latency_p50" in names

    def test_list_metrics_by_category(self):
        # 按分类过滤
        mc = MetricsCollector()
        mc.record_metric("quality.x", 1.0)
        mc.record_metric("efficiency.y", 2.0)
        quality_metrics = mc.list_metrics(category="quality")
        assert "quality.x" in quality_metrics
        assert "efficiency.y" not in quality_metrics


# =====================================================================
# METRIC_CATEGORIES - 11 大类
# =====================================================================


class TestMetricCategories:
    """测试 METRIC_CATEGORIES 11 大类"""

    def test_categories_count(self):
        # 11 大类
        assert len(METRIC_CATEGORIES) == 11

    def test_expected_categories(self):
        expected = {
            "quality",
            "efficiency",
            "knowledge",
            "safety",
            "cross_platform",
            "collaboration",
            "memory",
            "interop",
            "alignment",
            "resilience",
            "hallucination",
        }
        assert set(METRIC_CATEGORIES.keys()) == expected

    def test_each_category_has_metadata(self):
        # 每个分类有 name_cn / dashboard / description / metrics
        for cat, meta in METRIC_CATEGORIES.items():
            assert "name_cn" in meta, f"{cat} 缺 name_cn"
            assert "dashboard" in meta, f"{cat} 缺 dashboard"
            assert "metrics" in meta, f"{cat} 缺 metrics"
            assert isinstance(meta["metrics"], list)
            assert len(meta["metrics"]) > 0


# =====================================================================
# MetricsCollector - 全局单例与看板
# =====================================================================


class TestMetricsDashboard:
    """测试 MetricsCollector 看板"""

    def test_get_dashboard_returns_all_categories(self):
        # get_dashboard 返回所有 11 个分类
        mc = MetricsCollector()
        mc.record_metric("quality.x", 1.0)
        dashboard = mc.get_dashboard()
        for cat in METRIC_CATEGORIES:
            assert cat in dashboard

    def test_get_category(self):
        # get_category 返回单个分类视图
        # 注意：metrics 字典的键为完整指标名（含分类前缀），如 "quality.rule_violation_rate"
        mc = MetricsCollector()
        mc.record_metric("quality.rule_violation_rate", 0.1)
        cat_view = mc.get_category("quality")
        assert cat_view["name_cn"] == "质量"
        assert "quality.rule_violation_rate" in cat_view["metrics"]

    def test_get_category_unknown(self):
        # 未知分类返回默认结构
        mc = MetricsCollector()
        cat_view = mc.get_category("nonexistent")
        assert cat_view["name_cn"] == "未知分类"

    def test_global_singleton_exists(self):
        assert metrics_collector is not None
        assert isinstance(metrics_collector, MetricsCollector)
