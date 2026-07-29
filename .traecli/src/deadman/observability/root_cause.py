"""P6.1 失败根因自动归因 - 基于 trace span 树 + LLM 分析失败根因

参考 DEADMAN_UPGRADE_PLAN.md v1.2 P6 实施细化。

工作流程：
  1. 遍历 trace span 树，找首个 status=ERROR 的 span
  2. 提取 span events + attributes 作为上下文
  3. LLM 分析根因（chat_json 输出 root_cause + contributing_factors + suggested_fix）
  4. 关联相似历史失败（从 reflexion_memory_store 按 error_span_name 查询）

Feature flag: DEADMAN_ROOT_CAUSE_ENABLED=0 默认关闭
  - 关闭时 analyze 返回空报告（不抛异常）
  - 开启时执行 LLM 根因分析

降级路径全覆盖：
  1. feature flag 关闭 → 返回空报告
  2. trace 不存在 → 返回空报告
  3. trace 无 ERROR span → 返回空报告
  4. LLM 调用失败 → 用 rule-based fallback 给出兜底根因
  5. reflexion_memory_store 不可用 → similar_history 为空
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
ROOT_CAUSE_ENABLED: bool = os.environ.get(
    "DEADMAN_ROOT_CAUSE_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class RootCauseReport:
    """根因分析报告

    Attributes:
        trace_id: 关联的 trace ID
        error_span_id: 首个 ERROR span 的 ID（无错误时为空）
        error_span_name: 首个 ERROR span 的名称
        root_cause: LLM 分析出的根因描述
        contributing_factors: 促成因素列表
        similar_history: 相似历史失败记录（来自 reflexion_memory_store）
        suggested_fix: 建议修复方案
        timestamp: 报告生成时间
    """

    trace_id: str = ""
    error_span_id: str = ""
    error_span_name: str = ""
    root_cause: str = ""
    contributing_factors: list[str] = field(default_factory=list)
    similar_history: list[dict[str, Any]] = field(default_factory=list)
    suggested_fix: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# =====================================================================
# 根因分析器
# =====================================================================


class RootCauseAnalyzer:
    """失败根因自动归因器

    用法：

        analyzer = RootCauseAnalyzer(tracer, llm_client, reflexion_memory_store)
        report = await analyzer.analyze(trace_id)
        print(analyzer.format_report(report))

    feature flag 关闭时 analyze 返回空 RootCauseReport，不抛异常。
    """

    # LLM 评审 prompt 模板
    _ANALYZE_PROMPT = """你是失败根因分析专家。基于以下 trace span 上下文，分析失败根因。

## 失败 span 信息
- span 名称：{span_name}
- span 类型：{span_type}
- status：{status}

## span attributes
{attributes}

## span events
{events}

## 任务
1. 找出导致失败的根因（root cause，单一最关键原因）
2. 列出促成因素（contributing factors，辅助原因，最多 5 个）
3. 给出可执行的修复建议（suggested fix）

## 输出 JSON（严格遵守，不要输出其他内容）
{{
  "root_cause": "根因描述",
  "contributing_factors": ["因素1", "因素2"],
  "suggested_fix": "修复建议"
}}
"""

    def __init__(
        self,
        tracer: Any,
        llm_client: Any,
        reflexion_memory_store: Any = None,
    ) -> None:
        """
        Args:
            tracer: Tracer 实例（用于 get_trace 加载 span 树）
            llm_client: LLMClient 实例（用于 chat_json 分析根因）
            reflexion_memory_store: 可选的反思记忆存储（需实现
                get_reflexion_memory 或类似接口，按 agent_name 查询历史失败）
        """
        self.tracer = tracer
        self.llm_client = llm_client
        self.reflexion_memory_store = reflexion_memory_store

    async def analyze(self, trace_id: str) -> RootCauseReport:
        """分析指定 trace 的失败根因

        Args:
            trace_id: 要分析的 trace ID

        Returns:
            RootCauseReport；feature flag 关闭/trace 不存在/无 ERROR span 时返回空报告
        """
        # 1. feature flag 关闭 → 空报告
        if not ROOT_CAUSE_ENABLED:
            logger.debug(
                "root_cause disabled (DEADMAN_ROOT_CAUSE_ENABLED=0), skip trace_id=%s",
                trace_id,
            )
            return RootCauseReport(trace_id=trace_id)

        # 2. 加载 trace span 树
        try:
            spans = self.tracer.get_trace(trace_id) if self.tracer else []
        except Exception as e:
            logger.warning("加载 trace %s 失败: %s", trace_id, e)
            return RootCauseReport(trace_id=trace_id)

        if not spans:
            logger.debug("trace %s 无 span，返回空报告", trace_id)
            return RootCauseReport(trace_id=trace_id)

        # 3. 找首个 ERROR span
        error_span = self._find_first_error_span(spans)
        if error_span is None:
            logger.debug("trace %s 无 ERROR span，返回空报告", trace_id)
            return RootCauseReport(trace_id=trace_id)

        # 4. LLM 分析根因（失败用 rule-based fallback）
        root_cause, contributing_factors, suggested_fix = await self._llm_analyze(
            error_span
        )

        # 5. 关联相似历史失败（reflexion_memory_store 不可用时为空）
        similar_history = await self._query_similar_history(error_span)

        return RootCauseReport(
            trace_id=trace_id,
            error_span_id=str(error_span.get("span_id", "")),
            error_span_name=str(error_span.get("name", "")),
            root_cause=root_cause,
            contributing_factors=contributing_factors,
            similar_history=similar_history,
            suggested_fix=suggested_fix,
        )

    def format_report(self, report: RootCauseReport) -> str:
        """把 RootCauseReport 格式化为人类可读文本

        Args:
            report: 根因分析报告

        Returns:
            多行字符串，便于打印/记录到日志
        """
        if not report.error_span_id:
            return (
                f"=== 根因分析报告 ===\n"
                f"trace_id: {report.trace_id}\n"
                f"状态: 未发现 ERROR span 或功能未启用\n"
                f"时间: {report.timestamp}\n"
            )

        lines = [
            "=== 根因分析报告 ===",
            f"trace_id: {report.trace_id}",
            f"error_span_id: {report.error_span_id}",
            f"error_span_name: {report.error_span_name}",
            f"时间: {report.timestamp}",
            "",
            f"【根因】{report.root_cause}",
            "",
            "【促成因素】",
        ]
        if report.contributing_factors:
            for i, factor in enumerate(report.contributing_factors, 1):
                lines.append(f"  {i}. {factor}")
        else:
            lines.append("  (无)")

        lines.append("")
        lines.append("【相似历史失败】")
        if report.similar_history:
            for i, hist in enumerate(report.similar_history, 1):
                lines.append(
                    f"  {i}. {hist.get('failure_type', 'unknown')}: "
                    f"{hist.get('failure_reason', '') or hist.get('failure_message', '')}"
                )
        else:
            lines.append("  (无)")

        lines.append("")
        lines.append(f"【建议修复】{report.suggested_fix}")
        lines.append("")
        lines.append("=== 报告结束 ===")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _find_first_error_span(spans: list[dict[str, Any]]) -> dict[str, Any] | None:
        """从 span 列表中找首个 status=ERROR 的 span

        按 span 创建顺序遍历（spans 已是创建顺序），返回首个 ERROR span。
        """
        for span in spans:
            status = str(span.get("status", "")).upper()
            if status == "ERROR":
                return span
        return None

    async def _llm_analyze(
        self, error_span: dict[str, Any]
    ) -> tuple[str, list[str], str]:
        """调 LLM 分析根因，失败时降级为 rule-based fallback

        Returns:
            (root_cause, contributing_factors, suggested_fix)
        """
        prompt = self._build_prompt(error_span)
        try:
            messages = [{"role": "user", "content": prompt}]
            result = await self.llm_client.chat_json(messages, temperature=0.2)

            root_cause = str(result.get("root_cause", "")).strip()
            factors_raw = result.get("contributing_factors", [])
            if isinstance(factors_raw, list):
                contributing_factors = [str(f) for f in factors_raw if f]
            else:
                contributing_factors = [str(factors_raw)] if factors_raw else []
            suggested_fix = str(result.get("suggested_fix", "")).strip()

            # 兜底：LLM 漏字段时用 span 信息填充
            if not root_cause:
                root_cause = self._fallback_root_cause(error_span)
            if not suggested_fix:
                suggested_fix = self._fallback_suggested_fix(error_span)

            return root_cause, contributing_factors, suggested_fix
        except Exception as e:
            logger.warning("LLM 根因分析失败，降级为 rule-based: %s", e)
            return (
                self._fallback_root_cause(error_span),
                [],
                self._fallback_suggested_fix(error_span),
            )

    def _build_prompt(self, error_span: dict[str, Any]) -> str:
        """构造 LLM 分析 prompt"""
        import json

        attrs = error_span.get("attributes", {}) or {}
        events = error_span.get("events", []) or []
        try:
            attrs_str = json.dumps(attrs, ensure_ascii=False, default=str)
        except Exception:
            attrs_str = str(attrs)
        try:
            events_str = json.dumps(events, ensure_ascii=False, default=str)
        except Exception:
            events_str = str(events)

        return self._ANALYZE_PROMPT.format(
            span_name=error_span.get("name", ""),
            span_type=error_span.get("span_type", ""),
            status=error_span.get("status", ""),
            attributes=attrs_str,
            events=events_str,
        )

    @staticmethod
    def _fallback_root_cause(error_span: dict[str, Any]) -> str:
        """LLM 不可用时的兜底根因（基于 span 元数据规则推断）"""
        span_type = error_span.get("span_type", "")
        name = error_span.get("name", "")
        events = error_span.get("events", []) or []

        # 从 events 提取错误信息
        error_msg = ""
        for ev in events:
            if isinstance(ev, dict):
                if ev.get("error"):
                    error_msg = str(ev["error"])
                    break
                if ev.get("error_type"):
                    error_msg = f"{ev.get('error_type')}: {ev.get('error', '')}"
                    break

        if not error_msg:
            error_msg = f"span {name} ({span_type}) 状态为 ERROR"

        return f"[rule-based] {span_type} span '{name}' 失败：{error_msg}"

    @staticmethod
    def _fallback_suggested_fix(error_span: dict[str, Any]) -> str:
        """LLM 不可用时的兜底修复建议"""
        span_type = error_span.get("span_type", "")
        suggestions = {
            "tool": "检查工具参数与网络连通性，必要时降级到 fallback 工具",
            "agent": "检查智能体配置与 LLM 可用性，必要时触发 Reflexion 重试",
            "subagent": "检查子智能体 schema 与调用参数，必要时切换 fallback 子智能体",
            "transfer": "检查转介目标可用性与权限，必要时降级为人工转介",
            "reflexion": "检查重试策略与 fallback 路径，必要时增加预定义策略",
            "llm_judge": "检查评审模型可用性与共识阈值，必要时降级为正则/关键词判定",
        }
        return suggestions.get(
            span_type, "检查 span 上下文与依赖服务，必要时触发降级路径"
        )

    async def _query_similar_history(
        self, error_span: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """从 reflexion_memory_store 查询相似历史失败

        reflexion_memory_store 需实现 get_reflexion_memory(agent_name) 或类似接口。
        返回的 memory 结构（来自 ReflexionEngine._load_memory）：
            {
                "failure_patterns": {failure_type: count},
                "successful_adjustments": {failure_type: strategy},
            }
        本方法把 failure_patterns 转换为 similar_history list。

        不可用/异常时返回空列表。
        """
        if not self.reflexion_memory_store:
            return []

        # 用 span 名作为 agent_name 查询（与 ReflexionEngine 一致）
        agent_name = str(error_span.get("name", "unknown"))
        try:
            # 兼容 sync 和 async 两种实现
            memory: Any = None
            get_fn = getattr(self.reflexion_memory_store, "get_reflexion_memory", None)
            if get_fn is None:
                return []
            result = get_fn(agent_name)
            if hasattr(result, "__await__"):
                memory = await result  # type: ignore[misc]
            else:
                memory = result

            if not isinstance(memory, dict):
                return []

            failure_patterns = memory.get("failure_patterns", {}) or {}
            successful_adjustments = memory.get("successful_adjustments", {}) or {}

            history: list[dict[str, Any]] = []
            for failure_type, count in failure_patterns.items():
                if not isinstance(count, (int, float)) or count <= 0:
                    continue
                history.append(
                    {
                        "failure_type": str(failure_type),
                        "count": int(count),
                        "successful_adjustment": str(
                            successful_adjustments.get(failure_type, "")
                        ),
                    }
                )
            # 按出现次数倒序
            history.sort(key=lambda x: x.get("count", 0), reverse=True)
            return history
        except Exception as e:
            logger.warning("查询相似历史失败失败: %s", e)
            return []
