"""Tracer - 基于 OpenTelemetry 的多智能体 trace 记录器

参考 observability/Span-Model.md 与 observability/OTel-Integration-Guide.md 设计。

支持 11 类 span：root / agent / subagent / transfer / rule / tool / debate /
memory / a2a / reflexion / llm_judge。

OpenTelemetry 为可选依赖：若不可用则降级为内存 span 记录（dict 列表），
保证核心业务在无 OTel 环境下仍可工作。
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# === OpenTelemetry 可选依赖探测 ===
# 任何一项导入失败即降级为内存模式
OTEL_AVAILABLE = False
otel_trace = None
otel_trace_module = None
otel_status_module = None
otel_span_kind_module = None
otel_otlp_module = None
otel_sdk_module = None

try:
    from opentelemetry import trace as _otel_trace  # type: ignore
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
    from opentelemetry.trace import SpanKind, Status, StatusCode  # type: ignore

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )

        _otel_otlp_available = True
    except ImportError:  # pragma: no cover - exporter 可选
        OTLPSpanExporter = None  # type: ignore
        _otel_otlp_available = False

    otel_trace = _otel_trace
    otel_trace_module = _otel_trace
    otel_status_module = (Status, StatusCode)
    otel_span_kind_module = SpanKind
    otel_otlp_module = OTLPSpanExporter
    otel_sdk_module = (TracerProvider, BatchSpanProcessor)
    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 降级路径
    OTEL_AVAILABLE = False


class SpanType(str, Enum):
    """11 类 span 类型 - 对应 Span-Model.md v1.1"""

    ROOT = "root"  # 用户请求根 span
    AGENT = "agent"  # 并列智能体处理
    SUBAGENT = "subagent"  # 父智能体调用私有子智能体
    TRANSFER = "transfer"  # 智能体间转介
    RULE = "rule"  # 规则优先级链裁决
    TOOL = "tool"  # 工具调用（WebSearch/Read/...）
    DEBATE = "debate"  # 多智能体辩论会话
    MEMORY = "memory"  # 分层记忆查询/更新
    A2A = "a2a"  # 跨厂商 A2A 协议调用
    REFLEXION = "reflexion"  # 反思-调整-重试
    LLM_JUDGE = "llm_judge"  # LLM-as-Judge 评审


# span_type -> OTel SpanKind 映射
_SPAN_KIND_MAP: dict[str, Any] = {}


def _setup_otel_provider() -> Any | None:
    """初始化 OTel TracerProvider 并配置 OTLP exporter。

    指向 settings.otel_endpoint。返回 tracer 实例，失败返回 None。
    仅在 OTel SDK 可用时调用。
    """
    if not OTEL_AVAILABLE:
        return None
    assert otel_trace_module is not None  # narrowed by OTEL_AVAILABLE

    # 避免重复初始化：若已有非代理 provider，直接复用
    existing = otel_trace_module.get_tracer_provider()
    if existing is not None and not isinstance(existing, type):
        # 已有 provider，但可能是 NoOpTracerProvider，尝试替换为真实 provider
        provider_name = type(existing).__name__
        if provider_name == "TracerProvider":
            return otel_trace_module.get_tracer("deadman")

    try:
        provider = TracerProvider()
        if _otel_otlp_available and otel_otlp_module is not None:
            exporter = otel_otlp_module(endpoint=settings.otel_endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        otel_trace_module.set_tracer_provider(provider)
        return otel_trace_module.get_tracer("deadman")
    except Exception:  # pragma: no cover - 初始化失败降级
        return None


def _to_otel_value(value: Any) -> Any:
    """将任意 Python 值转为 OTel attribute 兼容形式。

    OTel 仅支持 str/int/float/bool 及其列表；复杂结构序列化为 JSON 字符串。
    """
    if value is None:
        return ""
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        # 列表内元素需同类型且为基础类型
        items = [_to_otel_value(v) for v in value]
        if items and all(isinstance(i, str | int | float | bool) for i in items):
            return items
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, dict):
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _status_to_otel(status: str) -> Any:
    """将平台 status 字符串映射为 OTel Status。"""
    if not OTEL_AVAILABLE or otel_status_module is None:
        return None
    Status, StatusCode = otel_status_module
    upper = (status or "").upper()
    if upper == "ERROR":
        return Status(StatusCode.ERROR, description=status)
    if upper == "OK":
        return Status(StatusCode.OK)
    # PARTIAL / FALLBACK / TIMEOUT / DECLINED / REJECTED 等记为 OK 并保留原 status 文本
    return Status(StatusCode.OK, description=status)


class Tracer:
    """多智能体 trace 记录器。

    工作模式：
    - OTel 可用：start_span/end_span 同时维护内存 span dict 与 OTel span，
      OTel span 通过 BatchSpanProcessor 异步导出到 settings.otel_endpoint。
    - OTel 不可用：仅维护内存 span dict 列表，调用方可通过 get_spans() 取出后写 JSONL。
    - Langfuse 可用（可选）：end_span 时同步记录到 Langfuse，用于 LLM 调用追踪。

    线程模型：单进程内使用，跨线程共享时建议加锁（此处未加锁，遵循 OTel 默认语义）。
    """

    def __init__(self) -> None:
        self.otel_available = OTEL_AVAILABLE
        self._otel_tracer: Any | None = None
        if self.otel_available:
            self._otel_tracer = _setup_otel_provider()
            if self._otel_tracer is None:
                # SDK 初始化失败，降级为内存模式
                self.otel_available = False

        # Langfuse 可选集成
        self._langfuse: Any | None = None
        self._init_langfuse()

        # 内存 span 存储：span_id -> span_dict
        self._spans: dict[str, dict[str, Any]] = {}
        # span_id -> OTel span 对象（仅 OTel 模式下有值）
        self._otel_spans: dict[str, Any] = {}
        # 当前活跃 span 栈（用于自动设置 parent_span_id）
        self._span_stack: list[str] = []
        # trace_id -> [span_id, ...] 索引，便于按 trace 取出全部 span
        self._trace_index: dict[str, list[str]] = {}

    def _init_langfuse(self) -> None:
        """初始化 Langfuse 客户端（可选）

        需要 LANGFUSE_HOST + LANGFUSE_SECRET_KEY + LANGFUSE_PUBLIC_KEY 配置。
        """
        if not settings.langfuse_host:
            return
        try:
            from langfuse import Langfuse  # type: ignore

            self._langfuse = Langfuse(
                host=settings.langfuse_host,
                secret_key=settings.langfuse_secret,
                public_key=settings.langfuse_public,
            )
        except ImportError:
            # langfuse 包未安装，跳过
            pass
        except Exception:
            # 初始化失败，降级为无 Langfuse
            self._langfuse = None

    # === 核心 API ===

    def start_span(
        self,
        span_type: SpanType | str,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        """启动一个 span，返回 span_id。

        - 自动根据当前活跃 span 栈设置 parent_span_id
        - 若栈为空且 span_type != root，仍会创建（parent_span_id=None）
        - 同时创建内存记录与 OTel span（如可用）
        """
        span_type_str = span_type.value if isinstance(span_type, SpanType) else str(span_type)
        span_id = str(uuid.uuid4())

        # 父 span 与 trace_id 推导
        parent_span_id = self._span_stack[-1] if self._span_stack else None
        if parent_span_id and parent_span_id in self._spans:
            trace_id = self._spans[parent_span_id]["trace_id"]
        else:
            # 新 trace：root span 或无父的孤儿 span
            trace_id = str(uuid.uuid4())

        span_dict: dict[str, Any] = {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": name,
            "span_type": span_type_str,
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "attributes": dict(attributes) if attributes else {},
            "status": None,
            "events": [],
        }
        self._spans[span_id] = span_dict
        self._span_stack.append(span_id)
        self._trace_index.setdefault(trace_id, []).append(span_id)

        # OTel span
        if self.otel_available and self._otel_tracer is not None:
            kind = _SPAN_KIND_MAP.get(span_type_str)
            try:
                if kind is not None:
                    otel_span = self._otel_tracer.start_as_current_span(name, kind=kind)
                else:
                    otel_span = self._otel_tracer.start_as_current_span(name)
                # 写入属性
                if attributes:
                    for k, v in attributes.items():
                        try:
                            otel_span.set_attribute(k, _to_otel_value(v))
                        except Exception as e:
                            logger.debug("OTel set_attribute 失败: %s", e)
                # 标记 span_type 便于后端筛选
                try:
                    otel_span.set_attribute("span.type", span_type_str)
                except Exception as e:
                    logger.debug("OTel set_attribute(span.type) 失败: %s", e)
                self._otel_spans[span_id] = otel_span
            except Exception:  # pragma: no cover - OTel 内部异常不阻塞业务
                self._otel_spans.pop(span_id, None)

        return span_id

    def end_span(
        self,
        span_id: str,
        status: str = "OK",
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        """结束一个 span。

        - 写入 end_time / status / events
        - 从活跃栈中弹出
        - 结束对应 OTel span（如可用）
        """
        span_dict = self._spans.get(span_id)
        if span_dict is None:
            return

        span_dict["end_time"] = datetime.now().isoformat()
        span_dict["status"] = status
        if events:
            span_dict["events"].extend(events)

        # 从栈中弹出（注意：若中间 span 被提前结束，需弹出其上方所有 span）
        while self._span_stack and self._span_stack[-1] != span_id:
            self._span_stack.pop()
        if self._span_stack and self._span_stack[-1] == span_id:
            self._span_stack.pop()

        # 结束 OTel span
        otel_span = self._otel_spans.pop(span_id, None)
        if otel_span is not None:
            try:
                # 追加 events
                if events:
                    for ev in events:
                        ev_name = str(ev.get("name", "event"))
                        ev_attrs = {k: _to_otel_value(v) for k, v in ev.items() if k != "name"}
                        try:
                            otel_span.add_event(ev_name, attributes=ev_attrs)
                        except Exception as e:
                            logger.debug("OTel add_event 失败: %s", e)
                # 设置状态
                otel_status = _status_to_otel(status)
                if otel_status is not None:
                    otel_span.set_status(otel_status)
                otel_span.end()
            except Exception as e:  # pragma: no cover
                logger.debug("OTel end_span 失败: %s", e)

        # Langfuse 同步（LLM 相关 span 记录为 generation）
        if self._langfuse is not None:
            self._sync_to_langfuse(span_dict)

    def _sync_to_langfuse(self, span_dict: dict[str, Any]) -> None:
        """将 span 同步到 Langfuse

        LLM_JUDGE / TOOL / AGENT 类 span 记录为 generation，
        其他类 span 记录为 trace event。
        """
        if self._langfuse is None:
            return
        try:
            span_type = span_dict.get("span_type", "")
            name = span_dict.get("name", "")
            attrs = span_dict.get("attributes", {})
            # LLM 调用类 span 记录为 generation
            if span_type in ("llm_judge", "tool", "agent"):
                self._langfuse.generation(
                    name=name,
                    metadata={
                        "span_type": span_type,
                        "trace_id": span_dict.get("trace_id"),
                        "span_id": span_dict.get("span_id"),
                        "status": span_dict.get("status"),
                    },
                    input=attrs.get("input"),
                    output=attrs.get("output"),
                    model=attrs.get("model", "unknown"),
                )
            else:
                # 其他 span 记录为 event
                self._langfuse.event(
                    name=name,
                    metadata={
                        "span_type": span_type,
                        "trace_id": span_dict.get("trace_id"),
                        "status": span_dict.get("status"),
                    },
                )
        except Exception:  # pragma: no cover - Langfuse 失败不影响主流程
            pass

    def emit_span(self, span_dict: dict[str, Any]) -> None:
        """将一个完整的 span 字典转为 OTel span 或内存记录。

        适用于：
        - 从 JSONL 文件回放历史 trace
        - 从其他平台（TRAE/OpenAI Agents SDK）原生 trace 适配导入
        - 测试构造场景

        span_dict 应包含：trace_id, span_id, parent_span_id, name, span_type,
        start_time, end_time, attributes, status, events。
        """
        span_id = span_dict.get("span_id") or str(uuid.uuid4())
        normalized = dict(span_dict)
        normalized.setdefault("trace_id", str(uuid.uuid4()))
        normalized.setdefault("span_id", span_id)
        normalized.setdefault("parent_span_id", None)
        normalized.setdefault("name", "emitted_span")
        normalized.setdefault("span_type", "tool")
        normalized.setdefault("start_time", datetime.now().isoformat())
        normalized.setdefault("end_time", None)
        normalized.setdefault("attributes", {})
        normalized.setdefault("status", "OK")
        normalized.setdefault("events", [])

        # 写入内存索引
        self._spans[span_id] = normalized
        trace_id = normalized["trace_id"]
        self._trace_index.setdefault(trace_id, []).append(span_id)

        # OTel 模式：尝试创建一个独立 span（无法精确还原 parent，仅作记录）
        if self.otel_available and self._otel_tracer is not None:
            try:
                with self._otel_tracer.start_as_current_span(normalized["name"]) as otel_span:
                    attrs = normalized.get("attributes", {}) or {}
                    for k, v in attrs.items():
                        try:
                            otel_span.set_attribute(k, _to_otel_value(v))
                        except Exception as e:
                            logger.debug("OTel set_attribute 失败: %s", e)
                    try:
                        otel_span.set_attribute("span.type", normalized.get("span_type", "tool"))
                    except Exception as e:
                        logger.debug("OTel set_attribute(span.type) 失败: %s", e)
                    for ev in normalized.get("events", []) or []:
                        ev_name = str(ev.get("name", "event"))
                        ev_attrs = {k: _to_otel_value(v) for k, v in ev.items() if k != "name"}
                        try:
                            otel_span.add_event(ev_name, attributes=ev_attrs)
                        except Exception as e:
                            logger.debug("OTel add_event 失败: %s", e)
                    otel_status = _status_to_otel(normalized.get("status", "OK"))
                    if otel_status is not None:
                        otel_span.set_status(otel_status)
            except Exception as e:  # pragma: no cover
                logger.debug("OTel span 同步失败: %s", e)

    # === 查询 API ===

    def get_span(self, span_id: str) -> dict[str, Any] | None:
        """获取单个 span 字典。"""
        return self._spans.get(span_id)

    def get_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """获取一个 trace 下的所有 span（按创建顺序）。"""
        span_ids = self._trace_index.get(trace_id, [])
        return [self._spans[sid] for sid in span_ids if sid in self._spans]

    def get_spans(self) -> list[dict[str, Any]]:
        """获取所有内存 span（按创建顺序）。"""
        return list(self._spans.values())

    def clear(self) -> None:
        """清空内存 span 记录（不影响已导出到 OTel 后端的 span）。"""
        self._spans.clear()
        self._otel_spans.clear()
        self._span_stack.clear()
        self._trace_index.clear()

    @property
    def current_span_id(self) -> str | None:
        """当前活跃栈顶 span_id。"""
        return self._span_stack[-1] if self._span_stack else None


# === 全局单例 ===
tracer = Tracer()

# 初始化 span_type -> SpanKind 映射（OTel 可用时）
if OTEL_AVAILABLE and otel_span_kind_module is not None:
    _SPAN_KIND_MAP = {
        SpanType.ROOT.value: otel_span_kind_module.SERVER,
        SpanType.AGENT.value: otel_span_kind_module.INTERNAL,
        SpanType.SUBAGENT.value: otel_span_kind_module.INTERNAL,
        SpanType.TRANSFER.value: otel_span_kind_module.INTERNAL,
        SpanType.RULE.value: otel_span_kind_module.INTERNAL,
        SpanType.TOOL.value: otel_span_kind_module.CLIENT,
        SpanType.DEBATE.value: otel_span_kind_module.INTERNAL,
        SpanType.MEMORY.value: otel_span_kind_module.CLIENT,
        SpanType.A2A.value: otel_span_kind_module.CLIENT,
        SpanType.REFLEXION.value: otel_span_kind_module.INTERNAL,
        SpanType.LLM_JUDGE.value: otel_span_kind_module.INTERNAL,
    }


# === 全局 trace_*_span 辅助函数 ===


class _ToolSpanContext:
    """工具调用 span 上下文管理器 + 装饰器。

    用法一（上下文管理器）：
        with trace_tool_span("web_search", {"query": "..."}):
            results = web_search(...)

    用法二（装饰器）：
        @trace_tool_span("web_search")
        def web_search(query): ...

    异常时自动以 ERROR 状态结束 span 并记录事件。
    """

    def __init__(
        self,
        tool_name: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.attributes = dict(attributes) if attributes else {}
        self.span_id: str | None = None

    def __enter__(self) -> str:
        attrs = {"tool_name": self.tool_name, **self.attributes}
        self.span_id = tracer.start_span(
            SpanType.TOOL,
            f"tool.{self.tool_name}",
            attrs,
        )
        return self.span_id

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.span_id is None:
            return
        if exc_type is None:
            tracer.end_span(self.span_id, status="OK")
        else:
            tracer.end_span(
                self.span_id,
                status="ERROR",
                events=[
                    {
                        "name": "exception",
                        "error_type": exc_type.__name__ if exc_type else "Unknown",
                        "error": str(exc_val) if exc_val else "",
                    }
                ],
            )
        return  # 不吞异常

    def __call__(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _ToolSpanContext(self.tool_name, self.attributes) as _sid:
                return func(*args, **kwargs)

        return wrapper


def trace_tool_span(
    tool_name: str,
    attributes: dict[str, Any] | None = None,
) -> _ToolSpanContext:
    """工具调用 span 辅助函数 - 返回可作上下文管理器或装饰器的对象。

    对应 Span-Model.md 第 6 类 Tool Span。
    """
    return _ToolSpanContext(tool_name, attributes)


class _ReflexionSpanContext:
    """反思重试 span 上下文管理器 + 装饰器。

    对应 Span-Model.md 第 10 类 Reflexion Span。
    典型属性：operation_type, operation_name, failure_reason,
    attempts_made, max_retries, success, fallback_used, adjustments_applied,
    strategy_used, graphiti_learned。
    """

    def __init__(self, attributes: dict[str, Any] | None = None) -> None:
        self.attributes = dict(attributes) if attributes else {}
        self.span_id: str | None = None

    def __enter__(self) -> str:
        name = self.attributes.get("operation_name", "reflexion.retry")
        self.span_id = tracer.start_span(
            SpanType.REFLEXION,
            f"reflexion.{name}" if not name.startswith("reflexion") else name,
            self.attributes,
        )
        return self.span_id

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.span_id is None:
            return
        if exc_type is None:
            # 由调用方在 with 块内通过 set_attribute 设置 success 等字段
            tracer.end_span(self.span_id, status="OK")
        else:
            tracer.end_span(
                self.span_id,
                status="ERROR",
                events=[
                    {
                        "name": "attempt_failed",
                        "error_type": exc_type.__name__ if exc_type else "Unknown",
                        "error": str(exc_val) if exc_val else "",
                    }
                ],
            )
        return

    def __call__(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _ReflexionSpanContext(self.attributes) as _sid:
                return func(*args, **kwargs)

        return wrapper


def trace_reflexion_span(
    attributes: dict[str, Any] | None = None,
) -> _ReflexionSpanContext:
    """反思重试 span 辅助函数 - 返回可作上下文管理器或装饰器的对象。

    对应 Span-Model.md 第 10 类 Reflexion Span（v1.1 新增）。
    """
    return _ReflexionSpanContext(attributes)


@contextmanager
def trace_root_span(
    name: str = "user_request",
    attributes: dict[str, Any] | None = None,
):
    """根 span 上下文管理器快捷函数。

    对应 Span-Model.md 第 1 类 Root Span。
    """
    span_id = tracer.start_span(SpanType.ROOT, name, attributes)
    try:
        yield span_id
        tracer.end_span(span_id, status="OK")
    except Exception as e:
        tracer.end_span(
            span_id,
            status="ERROR",
            events=[
                {
                    "name": "exception",
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            ],
        )
        raise


@contextmanager
def trace_agent_span(
    agent_name: str,
    attributes: dict[str, Any] | None = None,
):
    """智能体 span 上下文管理器快捷函数。

    对应 Span-Model.md 第 2 类 Agent Span。
    """
    attrs = {"agent_name": agent_name, **(attributes or {})}
    span_id = tracer.start_span(SpanType.AGENT, f"agent.{agent_name}", attrs)
    try:
        yield span_id
        tracer.end_span(span_id, status="OK")
    except Exception as e:
        tracer.end_span(
            span_id,
            status="ERROR",
            events=[
                {
                    "name": "exception",
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            ],
        )
        raise
