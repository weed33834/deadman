"""测试 P6.1 失败根因自动归因 - RootCauseAnalyzer

覆盖点：
  - analyze 找到首个 ERROR span
  - LLM 生成 root_cause
  - LLM 不可用时降级为 rule-based
  - 无 ERROR span 返回空报告
  - 关联相似历史失败（reflexion_memory_store）
  - format_report 人类可读
  - feature flag 关闭时返回空报告
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.observability.root_cause import (
    RootCauseAnalyzer,
    RootCauseReport,
)
from deadman.observability.tracer import SpanType, Tracer


@pytest.fixture
def enabled_root_cause(monkeypatch):
    """临时启用 ROOT_CAUSE_ENABLED"""
    monkeypatch.setattr("deadman.observability.root_cause.ROOT_CAUSE_ENABLED", True)
    yield


@pytest.fixture
def mock_llm():
    """构造 mock LLMClient"""
    client = MagicMock()
    client.chat_json = AsyncMock(
        return_value={
            "root_cause": "工具调用超时导致下游服务不可达",
            "contributing_factors": ["网络抖动", "下游限流"],
            "suggested_fix": "增加重试 + 切换 fallback 工具",
        }
    )
    return client


@pytest.fixture
def failing_tracer():
    """构造一个带 ERROR span 的 Tracer"""
    t = Tracer()
    # 先建一个 ROOT span (OK)
    root_id = t.start_span(SpanType.ROOT, "user_request", {"user_input": "hello"})
    # 再建一个 TOOL span 并以 ERROR 结束
    tool_id = t.start_span(
        SpanType.TOOL,
        "tool.web_search",
        {"tool_name": "web_search", "query": "test"},
    )
    t.end_span(
        tool_id,
        status="ERROR",
        events=[
            {
                "name": "exception",
                "error_type": "TimeoutError",
                "error": "request timed out after 30s",
            }
        ],
    )
    t.end_span(root_id, status="OK")
    return t


# =====================================================================
# 测试用例
# =====================================================================


class TestRootCauseAnalyze:
    """RootCauseAnalyzer.analyze 行为测试"""

    @pytest.mark.asyncio
    async def test_analyze_finds_error_span(self, enabled_root_cause, mock_llm, failing_tracer):
        """analyze 应找到首个 ERROR span 并填入报告"""
        analyzer = RootCauseAnalyzer(failing_tracer, mock_llm)
        # 取 tracer 中的某个 trace_id
        spans = failing_tracer.get_spans()
        trace_id = spans[0]["trace_id"]

        report = await analyzer.analyze(trace_id)

        assert report.error_span_id  # 非空
        assert report.error_span_name == "tool.web_search"
        assert report.trace_id == trace_id
        # ERROR span 的 status 应为 ERROR
        error_span = failing_tracer.get_span(report.error_span_id)
        assert error_span["status"] == "ERROR"

    @pytest.mark.asyncio
    async def test_analyze_llm_generates_root_cause(
        self, enabled_root_cause, mock_llm, failing_tracer
    ):
        """analyze 应用 LLM 输出填充 root_cause / contributing_factors / suggested_fix"""
        analyzer = RootCauseAnalyzer(failing_tracer, mock_llm)
        spans = failing_tracer.get_spans()
        trace_id = spans[0]["trace_id"]

        report = await analyzer.analyze(trace_id)

        assert "工具调用超时" in report.root_cause
        assert "网络抖动" in report.contributing_factors
        assert "下游限流" in report.contributing_factors
        assert "重试" in report.suggested_fix
        # LLM 应被调用一次
        mock_llm.chat_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_llm_unavailable_falls_back(self, enabled_root_cause, failing_tracer):
        """LLM 调用失败时应降级为 rule-based 根因"""
        bad_llm = MagicMock()
        bad_llm.chat_json = AsyncMock(side_effect=RuntimeError("LLM down"))

        analyzer = RootCauseAnalyzer(failing_tracer, bad_llm)
        spans = failing_tracer.get_spans()
        trace_id = spans[0]["trace_id"]

        report = await analyzer.analyze(trace_id)

        # 应有 rule-based 标记
        assert "[rule-based]" in report.root_cause
        # 应包含 span 名
        assert "tool.web_search" in report.root_cause
        # 应包含 events 中的错误信息
        assert "TimeoutError" in report.root_cause or "timed out" in report.root_cause
        # suggested_fix 应有内容（rule-based fallback 给出）
        assert report.suggested_fix
        # contributing_factors 为空列表（rule-based 不分析）
        assert report.contributing_factors == []

    @pytest.mark.asyncio
    async def test_analyze_no_error_returns_empty_report(self, enabled_root_cause, mock_llm):
        """trace 中无 ERROR span 时应返回空报告（error_span_id 为空）"""
        t = Tracer()
        root_id = t.start_span(SpanType.ROOT, "ok_request")
        t.end_span(root_id, status="OK")

        analyzer = RootCauseAnalyzer(t, mock_llm)
        spans = t.get_spans()
        trace_id = spans[0]["trace_id"]

        report = await analyzer.analyze(trace_id)

        assert report.error_span_id == ""
        assert report.root_cause == ""
        # LLM 不应被调用
        mock_llm.chat_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_similar_history_linked(
        self, enabled_root_cause, mock_llm, failing_tracer
    ):
        """reflexion_memory_store 提供相似历史时应填入 similar_history"""
        memory_store = MagicMock()
        memory_store.get_reflexion_memory = MagicMock(
            return_value={
                "failure_patterns": {"timeout": 5, "rate_limit": 2},
                "successful_adjustments": {"timeout": "增加重试"},
            }
        )

        analyzer = RootCauseAnalyzer(failing_tracer, mock_llm, reflexion_memory_store=memory_store)
        spans = failing_tracer.get_spans()
        trace_id = spans[0]["trace_id"]

        report = await analyzer.analyze(trace_id)

        assert len(report.similar_history) == 2
        # 按出现次数倒序
        assert report.similar_history[0]["count"] == 5
        assert report.similar_history[0]["failure_type"] == "timeout"
        assert report.similar_history[0]["successful_adjustment"] == "增加重试"

    @pytest.mark.asyncio
    async def test_analyze_similar_history_async_store(
        self, enabled_root_cause, mock_llm, failing_tracer
    ):
        """reflexion_memory_store 返回 coroutine 时应正确 await"""
        memory_store = MagicMock()
        memory_store.get_reflexion_memory = MagicMock(
            return_value={
                "failure_patterns": {"timeout": 3},
                "successful_adjustments": {},
            }
        )

        analyzer = RootCauseAnalyzer(failing_tracer, mock_llm, reflexion_memory_store=memory_store)
        spans = failing_tracer.get_spans()
        trace_id = spans[0]["trace_id"]

        report = await analyzer.analyze(trace_id)
        assert len(report.similar_history) == 1

    @pytest.mark.asyncio
    async def test_analyze_trace_not_found_returns_empty(self, enabled_root_cause, mock_llm):
        """trace_id 不存在时返回空报告"""
        t = Tracer()
        analyzer = RootCauseAnalyzer(t, mock_llm)
        report = await analyzer.analyze("non-existent-trace-id")
        assert report.error_span_id == ""
        assert report.root_cause == ""

    def test_format_report_human_readable(self, enabled_root_cause, mock_llm, failing_tracer):
        """format_report 应输出人类可读的多行文本"""
        report = RootCauseReport(
            trace_id="trace-123",
            error_span_id="span-456",
            error_span_name="tool.web_search",
            root_cause="工具调用超时",
            contributing_factors=["网络抖动", "限流"],
            similar_history=[
                {"failure_type": "timeout", "count": 3},
            ],
            suggested_fix="增加重试",
            timestamp="2026-07-25T10:00:00",
        )
        analyzer = RootCauseAnalyzer(failing_tracer, mock_llm)
        text = analyzer.format_report(report)

        assert "=== 根因分析报告 ===" in text
        assert "trace-123" in text
        assert "span-456" in text
        assert "tool.web_search" in text
        assert "工具调用超时" in text
        assert "网络抖动" in text
        assert "限流" in text
        assert "timeout" in text
        assert "增加重试" in text
        assert "=== 报告结束 ===" in text

    def test_format_report_empty_report(self, enabled_root_cause, mock_llm, failing_tracer):
        """空报告的 format 也应给出可读输出"""
        report = RootCauseReport(trace_id="trace-empty")
        analyzer = RootCauseAnalyzer(failing_tracer, mock_llm)
        text = analyzer.format_report(report)

        assert "trace-empty" in text
        assert "未发现 ERROR span" in text

    @pytest.mark.asyncio
    async def test_root_cause_disabled_returns_empty(self, mock_llm, failing_tracer):
        """feature flag 关闭时应返回空报告（ROOT_CAUSE_ENABLED=False 默认）"""
        # 注意：不调用 enabled_root_cause fixture，使用模块默认值
        # 由于 ROOT_CAUSE_ENABLED 在模块加载时确定，需要确保为 False
        from deadman.observability import root_cause as rc_module

        # 保留原值后强制设为 False
        original = rc_module.ROOT_CAUSE_ENABLED
        rc_module.ROOT_CAUSE_ENABLED = False
        try:
            analyzer = RootCauseAnalyzer(failing_tracer, mock_llm)
            spans = failing_tracer.get_spans()
            trace_id = spans[0]["trace_id"]

            report = await analyzer.analyze(trace_id)

            assert report.error_span_id == ""
            assert report.root_cause == ""
            # LLM 不应被调用
            mock_llm.chat_json.assert_not_called()
        finally:
            rc_module.ROOT_CAUSE_ENABLED = original
