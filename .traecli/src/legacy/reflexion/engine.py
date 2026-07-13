"""Reflexion 反思重试引擎

参考 agents/Reflexion-Mechanism.md 的设计：
- 子智能体/工具/转介调用失败时，通过「反思-调整-重试」恢复可恢复的失败
- 预定义调整策略表（快速路径）+ LLM 反思（慢速路径）
- 跨会话反思记忆（可选，与 Graphiti 集成）
- trace span 发射（可选，与 observability.tracer 集成）
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from ..config import settings
from ..llm import llm_client
from ..types import ExecutionMode

logger = logging.getLogger(__name__)


# === trace span 发射器（若 observability 模块不可用则降级为 None） ===
# trace_reflexion_span 是上下文管理器工厂，接收 attributes dict，返回 _ReflexionSpanContext
# tracer 是全局单例，用于在 span 生命周期内追加事件/属性
try:
    from ..observability.tracer import trace_reflexion_span  # type: ignore
    from ..observability.tracer import tracer as _tracer  # type: ignore
except ImportError:
    trace_reflexion_span = None  # type: ignore
    _tracer = None  # type: ignore


# === 预定义调整策略表（10 种常见失败模式，快速路径，无需调 LLM） ===
ADJUSTMENT_STRATEGIES: dict[str, dict[str, Any]] = {
    "platform_not_supported": {
        "strategy": "改用 MCP server 工具替代子智能体",
        "alternative_tool": "check_integrity",
        "adjusted_params": {"use_mcp_tool": True, "fallback_to_native": True},
    },
    "timeout": {
        "strategy": "简化任务描述，减少上下文",
        "context_reduction": "只保留核心信息，去除历史",
        "adjusted_params": {"simplify_task": True, "max_context_length": 2000},
    },
    "format_error": {
        "strategy": "在 prompt 中加入更明确的输出格式要求",
        "format_spec": "请严格返回 JSON 格式：{\"field\": value}",
        "adjusted_params": {"enforce_format": True, "format_hint": "JSON"},
    },
    "subagent_not_found": {
        "strategy": "用 fallback 子智能体",
        "adjusted_params": {"use_fallback_subagent": True},
    },
    "knowledge_not_found": {
        "strategy": "触发 web_search",
        "adjusted_params": {"trigger_web_search": True},
    },
    "api_error": {
        "strategy": "降级到 fallback 模式",
        "adjusted_params": {"execution_mode": "fallback"},
    },
    "rate_limit": {
        "strategy": "等待后重试",
        "backoff_seconds": 60,
        "adjusted_params": {"backoff_seconds": 60},
    },
    "invalid_argument": {
        "strategy": "修正参数格式",
        "adjusted_params": {"auto_fix_args": True},
    },
    "permission_denied": {
        "strategy": "跳过该操作",
        "adjusted_params": {"skip_operation": True},
    },
    "unknown": {
        "strategy": "通用策略：简化输入重试",
        "adjusted_params": {"simplify_input": True},
    },
}


def get_predefined_strategy(failure_type: str) -> Optional[dict[str, Any]]:
    """获取预定义的调整策略（快速路径，不调 LLM）

    Args:
        failure_type: 失败模式标识

    Returns:
        命中则返回策略 dict，否则 None
    """
    return ADJUSTMENT_STRATEGIES.get(failure_type)


class ReflexionEngine:
    """Reflexion 反思重试引擎

    在子智能体/工具/转介调用失败时：
    1. 评估失败
    2. LLM 反思失败原因
    3. 查预定义策略表或用 LLM 生成调整
    4. 重试
    5. 重试耗尽则 fallback

    一个 engine 实例可复用（每次 execute_with_reflexion 会清空内部状态）。
    """

    def __init__(
        self,
        agent_name: str = "default",
        memory_store: Any = None,
    ) -> None:
        """
        Args:
            agent_name: 持有该 engine 的智能体名（用于跨会话记忆）
            memory_store: 可选的记忆存储客户端（如 Graphiti），需实现
                          get_reflexion_memory / record_successful_adjustment
        """
        self.agent_name = agent_name
        self.memory_store = memory_store
        # 最大重试次数从全局配置获取
        self.max_retries: int = settings.reflexion_max_retries
        # 本次执行过程中的失败与反思记录（每次 execute_with_reflexion 重置）
        self.failures: list[dict[str, Any]] = []
        self.reflections: list[dict[str, Any]] = []

    async def execute_with_reflexion(
        self,
        operation: Callable[..., Awaitable[Any]],
        initial_input: dict[str, Any],
        operation_type: str,
    ) -> dict[str, Any]:
        """带反思重试的操作执行器

        Args:
            operation: async callable，实际执行的操作（按 **input 调用）
            initial_input: 初始输入参数（dict）
            operation_type: 操作类型 - "subagent" / "tool" / "transfer"

        Returns:
            成功：{"success": True, "result": ..., "attempts": N}
            全部失败：{"success": False, "fallback": True, "attempts": MAX_RETRIES, ...}
        """
        # 每次执行前清空历史（engine 可复用）
        self.failures = []
        self.reflections = []

        current_input: dict[str, Any] = dict(initial_input)
        max_retries = self.max_retries

        # === 进入 reflexion trace span（若 observability 不可用则 span_id 为 None） ===
        # trace_reflexion_span 是上下文管理器，包裹整个反思-重试过程
        span_attrs = {
            "operation_type": operation_type,
            "operation_name": self._infer_operation_name(initial_input),
            "max_retries": max_retries,
            "agent_name": self.agent_name,
        }

        with ExitStack() as stack:
            span_id = self._enter_span(stack, span_attrs)

            for attempt in range(1, max_retries + 1):
                # 1. 执行操作
                failure_info: Optional[dict[str, Any]] = None
                try:
                    result = await operation(**current_input)

                    # 2. 评估结果
                    evaluation = await self._evaluate_result(result, operation_type)

                    if evaluation["success"]:
                        # 成功：若重试过，记录成功调整策略到跨会话记忆
                        if attempt > 1 and self.failures and self.reflections:
                            await self._record_successful_adjustment(
                                self.failures[-1].get("failure_type", "unknown"),
                                self.reflections[-1].get("adjustment_strategy", ""),
                            )
                        final_result = {
                            "success": True,
                            "result": result,
                            "attempts": attempt,
                        }
                        self._finalize_span(span_id, final_result)
                        return final_result

                    # 评估为失败 → 构造 failure_info
                    failure_info = {
                        "attempt": attempt,
                        "failure_type": evaluation.get("failure_type", "unknown"),
                        "failure_message": evaluation.get("failure_message", "未知失败"),
                        "input_summary": str(current_input)[:200],
                        "output_summary": str(result)[:200] if result is not None else None,
                        "timestamp": datetime.now().isoformat(),
                    }

                except Exception as e:
                    # 异常失败 → 构造 failure_info
                    logger.warning(
                        "Reflexion 第 %d 次执行抛出异常: %s", attempt, e, exc_info=True
                    )
                    failure_info = {
                        "attempt": attempt,
                        "failure_type": "exception",
                        "failure_message": f"{type(e).__name__}: {e}",
                        "input_summary": str(current_input)[:200],
                        "output_summary": None,
                        "timestamp": datetime.now().isoformat(),
                    }

                # 记录失败
                self.failures.append(failure_info)

                # 3. 最后一次尝试不再反思/调整，直接跳出进入 fallback
                if attempt >= max_retries:
                    break

                # 4. Reflexion：用 LLM 分析失败原因
                reflection = await self._reflect(failure_info, operation_type)
                self.reflections.append(reflection)

                # 5. 调整输入参数
                current_input = await self._adjust_input(current_input, reflection)

                # 6. 添加单次尝试的失败事件到 span（若 span 不可用则跳过）
                self._add_span_event(span_id, failure_info, reflection, attempt)

            # 重试耗尽 → fallback
            final_result = {
                "success": False,
                "fallback": True,
                "attempts": max_retries,
                "failures": self.failures,
                "reflections": self.reflections,
                "fallback_reason": self._determine_fallback_reason(),
            }
            self._finalize_span(span_id, final_result)
            return final_result

    async def _evaluate_result(
        self, result: Any, operation_type: str
    ) -> dict[str, Any]:
        """判断操作结果是否成功

        Args:
            result: 操作返回结果
            operation_type: "subagent" / "tool" / "transfer"

        Returns:
            成功：{"success": True}
            失败：{"success": False, "failure_type": ..., "failure_message": ...}
        """
        # 非 dict 结果：None 视为空响应失败，其他视为成功（无法判断）
        if not isinstance(result, dict):
            if result is None:
                return {
                    "success": False,
                    "failure_type": "empty_response",
                    "failure_message": "返回结果为空",
                }
            return {"success": True}

        if operation_type == "subagent":
            # 检查 execution_mode 是否为 SUCCESS
            execution_mode = result.get("execution_mode")
            # 兼容 ExecutionMode 枚举与字符串
            mode_value = (
                execution_mode.value
                if isinstance(execution_mode, ExecutionMode)
                else execution_mode
            )

            if mode_value == ExecutionMode.SUCCESS.value:
                return {"success": True}
            if mode_value == ExecutionMode.FALLBACK.value:
                return {
                    "success": False,
                    "failure_type": result.get("fallback_reason", "fallback"),
                    "failure_message": result.get("fallback_reason")
                    or "子智能体进入 fallback 模式",
                }
            return {
                "success": False,
                "failure_type": result.get("failure_type", "execution_failed"),
                "failure_message": result.get("error") or str(result)[:200],
            }

        if operation_type == "tool":
            # 检查是否有 error 字段
            if result.get("error"):
                return {
                    "success": False,
                    "failure_type": result.get("error_type", "tool_error"),
                    "failure_message": result.get("error", "工具调用出错"),
                }
            # 兼容 success 字段
            if "success" in result and not result["success"]:
                return {
                    "success": False,
                    "failure_type": result.get("error_type", "tool_error"),
                    "failure_message": result.get("error", "工具调用失败"),
                }
            return {"success": True}

        if operation_type == "transfer":
            # 检查是否成功发起转介
            if result.get("accepted") or result.get("transferred") or result.get("success"):
                return {"success": True}
            return {
                "success": False,
                "failure_type": result.get("failure_type", "transfer_failed"),
                "failure_message": result.get("reason")
                or result.get("error")
                or "转介失败",
            }

        # 未知 operation_type，默认成功（无法评估）
        return {"success": True}

    async def _reflect(
        self, failure_info: dict[str, Any], operation_type: str
    ) -> dict[str, Any]:
        """用 LLM 生成反思（什么原因失败、如何调整）

        Args:
            failure_info: 失败信息（含 attempt/failure_type/failure_message/input_summary/...）
            operation_type: 操作类型

        Returns:
            {
                "failure_type": str,           # LLM 重新分类后的失败模式
                "failure_reason": str,         # 失败根本原因
                "adjustment_strategy": str,    # 调整策略描述
                "adjusted_params": dict,       # 具体参数调整
            }
        """
        failure_type = failure_info.get("failure_type", "unknown")

        # 加载历史反思记忆（若有 memory_store）
        historical_pattern = 0
        historical_adjustment: Optional[str] = None
        memory = await self._load_memory()
        if memory:
            historical_pattern = (
                memory.get("failure_patterns", {}).get(failure_type, 0)
                if isinstance(memory, dict)
                else 0
            )
            historical_adjustment = (
                memory.get("successful_adjustments", {}).get(failure_type)
                if isinstance(memory, dict)
                else None
            )

        prompt = f"""你是 Reflexion 反思引擎。分析以下失败，生成调整策略。

## 操作类型
{operation_type}

## 失败记录
- 第 {failure_info.get('attempt', 1)} 次尝试
- 失败类型：{failure_type}
- 失败信息：{failure_info.get('failure_message', '')}
- 输入摘要：{failure_info.get('input_summary', '')}
- 输出摘要：{failure_info.get('output_summary') or '无'}

## 历史经验
- 此类失败历史出现次数：{historical_pattern}
- 历史成功调整策略：{historical_adjustment or '无'}

## 反思任务
1. 分析失败的根本原因（不要只看表面）
2. 重新分类失败模式（从下列选一个或自定义）：
   platform_not_supported / timeout / format_error / subagent_not_found /
   knowledge_not_found / api_error / rate_limit / invalid_argument /
   permission_denied / unknown
3. 生成调整策略（调整 prompt / 调整参数 / 切换策略）
4. 给出具体可执行的 adjusted_params

## 输出 JSON（严格遵守）
{{
  "failure_type": "{failure_type}",
  "failure_reason": "失败根本原因",
  "adjustment_strategy": "调整策略描述",
  "adjusted_params": {{}}
}}
"""
        try:
            messages = [{"role": "user", "content": prompt}]
            reflection = await llm_client.chat_json(messages, temperature=0.2)

            # 兜底字段（LLM 可能漏字段）
            reflection.setdefault("failure_type", failure_type)
            reflection.setdefault("failure_reason", failure_info.get("failure_message", ""))
            reflection.setdefault("adjustment_strategy", "")
            if not isinstance(reflection.get("adjusted_params"), dict):
                reflection["adjusted_params"] = {}
            return reflection

        except Exception as e:
            logger.warning("LLM 反思失败，使用兜底反思: %s", e)
            return {
                "failure_type": failure_type,
                "failure_reason": failure_info.get("failure_message", "未知失败"),
                "adjustment_strategy": "兜底策略：简化输入重试",
                "adjusted_params": {},
            }

    async def _adjust_input(
        self, current_input: dict[str, Any], reflection: dict[str, Any]
    ) -> dict[str, Any]:
        """根据反思调整输入参数

        流程：
        1. 快速路径：查预定义策略表 ADJUSTMENT_STRATEGIES，命中则用预定义参数
        2. 慢速路径：使用 _reflect 中 LLM 已生成的 adjusted_params
        3. 注入历史反思上下文到 prompt（避免重复犯错）

        Args:
            current_input: 当前输入参数
            reflection: _reflect 返回的反思结果

        Returns:
            调整后的新输入 dict（不修改入参）
        """
        adjusted = dict(current_input)
        failure_type = reflection.get("failure_type", "unknown")

        # 1. 快速路径：预定义策略表
        predefined = get_predefined_strategy(failure_type)
        if predefined:
            logger.info(
                "命中预定义调整策略 [%s]: %s",
                failure_type,
                predefined.get("strategy"),
            )
            adjusted_params = predefined.get("adjusted_params", {})
            if isinstance(adjusted_params, dict):
                adjusted.update(adjusted_params)
            # 注入反思上下文后返回
            return self._inject_reflection_context(adjusted)

        # 2. 慢速路径：使用 LLM 生成的 adjusted_params
        adjusted_params = reflection.get("adjusted_params", {})
        if isinstance(adjusted_params, dict) and adjusted_params:
            adjusted.update(adjusted_params)
        else:
            # adjusted_params 也为空 → 通用兜底：标记简化输入
            logger.info(
                "无预定义策略且 LLM 未给出 adjusted_params，使用通用兜底 [%s]",
                failure_type,
            )
            adjusted.setdefault("simplify_input", True)

        # 3. 注入历史反思上下文
        return self._inject_reflection_context(adjusted)

    def _inject_reflection_context(
        self, current_input: dict[str, Any]
    ) -> dict[str, Any]:
        """把最近的失败反思加入 prompt，避免重复犯错

        取最近 3 次失败与对应反思，拼接到 prompt/task 末尾。

        Args:
            current_input: 当前输入

        Returns:
            注入反思上下文后的输入（新 dict）
        """
        adjusted = dict(current_input)

        # 找到 prompt 字段（兼容 prompt / task / message）
        prompt_key: Optional[str] = None
        prompt_value: str = ""
        for key in ("prompt", "task", "message"):
            val = adjusted.get(key)
            if isinstance(val, str) and val:
                prompt_key = key
                prompt_value = val
                break

        if not prompt_key:
            return adjusted

        # 取最近 3 次（failures 与 reflections 已按顺序 append，长度可能不等）
        recent_failures = self.failures[-3:]
        recent_reflections = self.reflections[-3:]

        if not recent_failures or not recent_reflections:
            return adjusted

        reflection_context = "\n\n## 历史失败与反思（避免重复犯错）\n"
        for failure, reflection in zip(recent_failures, recent_reflections):
            reflection_context += (
                f"- 第 {failure.get('attempt', '?')} 次失败："
                f"{failure.get('failure_type', 'unknown')}\n"
                f"  反思：{reflection.get('failure_reason', '')}\n"
                f"  本次调整：{reflection.get('adjustment_strategy', '')}\n"
            )

        adjusted[prompt_key] = prompt_value + reflection_context
        return adjusted

    def _determine_fallback_reason(self) -> str:
        """重试耗尽后，决定 fallback 原因"""
        if not self.failures:
            return "unknown"

        last_failure = self.failures[-1]
        last_reflection = self.reflections[-1] if self.reflections else None

        reason = f"重试 {self.max_retries} 次后仍失败。"
        reason += (
            f"最后一次失败：{last_failure.get('failure_type', 'unknown')} - "
            f"{last_failure.get('failure_message', '')}"
        )
        if last_reflection:
            reason += f"。反思：{last_reflection.get('failure_reason', '')}"
        return reason

    async def _load_memory(self) -> Optional[dict[str, Any]]:
        """从 Graphiti 加载反思记忆（若 memory_store 不可用则返回 None）"""
        if not self.memory_store:
            return None
        try:
            return await self.memory_store.get_reflexion_memory(self.agent_name)
        except Exception as e:
            logger.warning("加载反思记忆失败: %s", e)
            return None

    async def _record_successful_adjustment(
        self, failure_type: str, adjustment_strategy: str
    ) -> None:
        """记录成功的调整策略到跨会话记忆（若 memory_store 不可用则跳过）"""
        if not self.memory_store:
            return
        try:
            await self.memory_store.record_successful_adjustment(
                agent_name=self.agent_name,
                failure_type=failure_type,
                adjustment_strategy=adjustment_strategy,
            )
        except Exception as e:
            logger.warning("记录成功调整策略失败: %s", e)

    # === trace span 辅助方法（observability 不可用时全部安全跳过） ===

    def _infer_operation_name(self, initial_input: dict[str, Any]) -> str:
        """从输入参数推断操作名（用于 span 命名）"""
        for key in ("subagent_name", "tool_name", "to_agent", "agent_name"):
            val = initial_input.get(key)
            if val:
                return str(val)
        return "retry"

    def _enter_span(
        self,
        stack: ExitStack,
        attrs: dict[str, Any],
    ) -> Optional[str]:
        """进入 reflexion trace span，返回 span_id（若不可用返回 None）

        使用 ExitStack 确保退出时自动调用 __exit__，即使业务逻辑抛异常。
        若 trace_reflexion_span 不可用或进入失败，安全降级为 None。
        """
        if trace_reflexion_span is None:
            return None
        try:
            return stack.enter_context(trace_reflexion_span(attrs))
        except Exception as e:
            logger.warning("进入 reflexion span 失败: %s", e)
            return None

    def _add_span_event(
        self,
        span_id: Optional[str],
        failure_info: dict[str, Any],
        reflection: dict[str, Any],
        attempt: int,
    ) -> None:
        """添加单次尝试的失败事件到 span（若 span 不可用则跳过）

        在 span 的 events 列表中追加一条记录，便于按尝试回溯。
        """
        if span_id is None or _tracer is None:
            return
        try:
            span = _tracer.get_span(span_id)
            if span is None:
                return
            span.setdefault("events", []).append(
                {
                    "name": f"attempt_{attempt}_failed",
                    "attempt": attempt,
                    "failure_type": failure_info.get("failure_type", "unknown"),
                    "failure_message": failure_info.get("failure_message", ""),
                    "failure_reason": reflection.get("failure_reason", ""),
                    "adjustment_strategy": reflection.get("adjustment_strategy", ""),
                }
            )
        except Exception as e:
            logger.warning("添加 span 事件失败: %s", e)

    def _finalize_span(
        self,
        span_id: Optional[str],
        result: dict[str, Any],
    ) -> None:
        """在 span 结束前更新最终属性（成功/失败、尝试次数等）

        在 ExitStack 退出前调用，将最终结果写入 span attributes，
        便于后端筛选和统计。
        """
        if span_id is None or _tracer is None:
            return
        try:
            span = _tracer.get_span(span_id)
            if span is None:
                return
            attrs = span.setdefault("attributes", {})
            attrs.update(
                {
                    "success": result.get("success", False),
                    "fallback_used": result.get("fallback", False),
                    "attempts_made": result.get("attempts", 0),
                }
            )
            if not result.get("success"):
                attrs["failure_reason"] = result.get("fallback_reason", "")
            if self.reflections:
                attrs["strategy_used"] = self.reflections[-1].get(
                    "adjustment_strategy", ""
                )
        except Exception as e:
            logger.warning("更新 span 最终属性失败: %s", e)
