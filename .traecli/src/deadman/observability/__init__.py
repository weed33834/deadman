"""可观测性模块 - 多智能体平台的 trace 与 metrics 采集

参考 observability/Span-Model.md（11 类 span）与 observability/Metrics.md（11 大类指标）。

设计要点：
- OpenTelemetry 为可选依赖：若不可用则降级为内存 span 记录。
- 提供 tracer 与 metrics_collector 全局单例，业务代码直接 import 使用。
- span 字典结构与 Span-Model.md JSON 示例对齐，便于序列化为 JSONL 持久化。

典型用法：

    from deadman.observability import (
        tracer,
        metrics_collector,
        SpanType,
        trace_tool_span,
        trace_reflexion_span,
    )

    # 1. 启动根 span
    root_id = tracer.start_span(SpanType.ROOT, "user_request", {"platform": "trae"})

    # 2. 工具调用 span（上下文管理器）
    with trace_tool_span("web_search", {"query": "北京户籍注销"}):
        results = web_search("北京户籍注销")

    # 3. 反思重试 span（装饰器）
    @trace_reflexion_span({"operation_type": "tool", "max_retries": 3})
    def retry_search(query): ...

    # 4. 结束根 span
    tracer.end_span(root_id, status="OK")

    # 5. 记录指标
    metrics_collector.record_metric(
        "quality.rule_violation_rate", 0.0, tags={"agent": "death-aftercare"}
    )

    # 6. 查看看板
    dashboard = metrics_collector.get_dashboard()
"""

from __future__ import annotations

from .metrics import METRIC_CATEGORIES, MetricsCollector, metrics_collector
from .tracer import (
    OTEL_AVAILABLE,
    SpanType,
    Tracer,
    tracer,
    trace_agent_span,
    trace_reflexion_span,
    trace_root_span,
    trace_tool_span,
)

__all__ = [
    # Tracer
    "Tracer",
    "SpanType",
    "tracer",
    "OTEL_AVAILABLE",
    "trace_tool_span",
    "trace_reflexion_span",
    "trace_root_span",
    "trace_agent_span",
    # Metrics
    "MetricsCollector",
    "METRIC_CATEGORIES",
    "metrics_collector",
]
