"""显式 ReAct 循环 - Thought → Action → Observation 三段式

P0.4 实现:在 agent_node 内部封装 ReAct 循环,把"单次 LLM 调用"升级为
"推理 → 工具调用 → 观察 → 继续推理"的迭代过程。

核心设计:
- Thought:LLM 推理下一步,输出 JSON {thought, action, action_input, final_answer?}
- Action:分发到注册的 MCP 工具(web_search / read_file / knowledge_lookup 等)
- Observation:工具结果注入 LLM 上下文,继续下一轮
- 终止:LLM 输出 final_answer,或达 max_iterations 后由 LLM 综合历史作答

韧性 / 安全特性(对齐 v1.1 深挖补充 P0.4):
- ★ 工具选择策略:维护 failed_tools 集合,失败工具本轮禁用,避免循环踩坑
- ★ Self-Verification:Action 后 LLM 自检 Observation 是否符合 Thought 假设;不符触发 Early Stop
- ★ 工具调用 budget:硬上限,超限直接综合历史作答,防无限工具链
- ★ Stuck 检测:连续 2 次 Observation Jaccard 相似度 > 0.9 → 视为 stuck,提前终止
- ★ Token 预算分层:单次迭代 max_tokens 上限,避免单轮 LLM 输出过长挤占后续轮次
- ★ Trace span:每个 Thought/Action/Observation 独立 span,可重放做轨迹级评测

Feature flag:DEADMAN_REACT_ENABLED=1 启用;默认 0 保留旧行为(单次 LLM 调用),
确保不破坏现有 918 测试。

降级路径:
- LLM 不可用 → 直接返回空字符串,由 agent_node 走旧降级分支
- 所有工具调用失败 → 跳过工具,让 LLM 直接基于已有上下文作答
- JSON 解析失败 → 回退到文本截取(final_answer 字段)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient, get_llm_for_use_case
from ..utils.text_similarity import jaccard_similarity as _jaccard_sim
from ..utils.text_similarity import tokenize as _tokenize

logger = logging.getLogger(__name__)

# =====================================================================
# 配置(全部 feature flag,默认安全)
# =====================================================================

# ReAct 总开关:默认关闭,保留 agent_node 旧行为
REACT_ENABLED: bool = os.environ.get("DEADMAN_REACT_ENABLED", "0").lower() in (
    "1", "true", "yes", "on",
)

# 最大迭代次数(Thought → Action → Observation 算一次)
REACT_MAX_ITERATIONS: int = int(os.environ.get("DEADMAN_REACT_MAX_ITERATIONS", "3"))

# 单轮工具调用 budget:超过则不再调工具,综合历史作答
REACT_TOOL_BUDGET: int = int(os.environ.get("DEADMAN_REACT_TOOL_BUDGET", "5"))

# 单次 LLM 迭代 max_tokens 上限(避免单轮输出挤占后续轮次)
REACT_ITERATION_MAX_TOKENS: int = int(
    os.environ.get("DEADMAN_REACT_ITERATION_MAX_TOKENS", "1500")
)

# Stuck 检测阈值:连续 2 次 Observation Jaccard 相似度 > 此值 → 提前终止
REACT_STUCK_JACCARD_THRESHOLD: float = float(
    os.environ.get("DEADMAN_REACT_STUCK_JACCARD", "0.9")
)

# Self-Verification 开关:Action 后让 LLM 校验 Observation 是否符合 Thought 假设
REACT_SELF_VERIFY: bool = os.environ.get(
    "DEADMAN_REACT_SELF_VERIFY", "0"
).lower() in ("1", "true", "yes", "on")

# P1.5: ReAct + Reflexion 联动开关 - 默认关闭，确保不破坏现有行为
# 启用时：ReAct 失败终止（stuck/self_verify_fail/max_iterations/error）后，
# 自动触发 ReflexionEngine 反思，注入反思上下文重试，最多
# REACT_REFLEXION_MAX_ROUNDS 轮。
REACT_REFLEXION_ENABLED: bool = os.environ.get(
    "DEADMAN_REACT_REFLEXION_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# P1.5: Reflexion 重试轮数上限
REACT_REFLEXION_MAX_ROUNDS: int = int(
    os.environ.get("DEADMAN_REACT_REFLEXION_MAX_ROUNDS", "2")
)

# P1.5: 触发 Reflexion 的 terminated_by 集合
# （final_answer 是正常终止，不触发；llm_unavailable 走降级路径，不触发）
REACT_REFLEXION_TRIGGERS: frozenset[str] = frozenset(
    {"stuck", "self_verify_fail", "max_iterations", "error"}
)


# =====================================================================
# 数据结构
# =====================================================================


@dataclass
class ReActStep:
    """单次 ReAct 迭代的完整记录(可序列化,用于 trace / 重放)"""

    iteration: int
    thought: str = ""
    action: str = ""           # 工具名 / "FINAL_ANSWER" / "NO_ACTION"
    action_input: dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    observation_raw: Any = None
    tool_ok: bool = True
    tool_error: str = ""
    self_verify_passed: bool | None = None
    tokens_used: int = 0


@dataclass
class ReActResult:
    """ReAct 循环的最终输出"""

    final_answer: str = ""
    steps: list[ReActStep] = field(default_factory=list)
    terminated_by: str = "max_iterations"  # final_answer / max_iterations / stuck / tool_budget / self_verify_fail / error
    total_tokens: int = 0
    degraded: bool = False  # LLM 不可用时降级
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "steps": [
                {
                    "iteration": s.iteration,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                    "tool_ok": s.tool_ok,
                    "tool_error": s.tool_error,
                    "self_verify_passed": s.self_verify_passed,
                    "tokens_used": s.tokens_used,
                }
                for s in self.steps
            ],
            "terminated_by": self.terminated_by,
            "total_tokens": self.total_tokens,
            "degraded": self.degraded,
            "note": self.note,
        }


# =====================================================================
# 工具注册表 - 暴露给 ReAct 的 Action 分发
# =====================================================================

# Action 名 → 异步 handler 的映射
# handler 接收 dict 参数,返回 Any(原始结果),由 _format_observation 转字符串
ToolHandler = Callable[..., Awaitable[Any]]
_TOOL_REGISTRY: dict[str, ToolHandler] = {}


def register_react_tool(name: str, handler: ToolHandler) -> None:
    """注册一个 ReAct 可调用的工具 handler。

    供 mcp_server / tools 模块在 import 时调用,把已有 MCP 工具的 async 函数
    注册到 ReAct 工具表。ReAct loop 只通过此表分发,不直接耦合 MCP 协议。
    """
    _TOOL_REGISTRY[name] = handler


def get_available_tools() -> list[str]:
    """返回当前注册的可用工具名清单(供 LLM prompt 展示)"""
    return sorted(_TOOL_REGISTRY.keys())


async def _dispatch_tool(
    name: str, action_input: dict[str, Any], failed_tools: set[str]
) -> tuple[bool, Any, str]:
    """分发工具调用,返回 (ok, result, error_msg)。

    失败工具加入 failed_tools,本轮后续禁用(工具选择策略)。
    未注册工具 / 异常都视为失败,不抛出(韧性优先)。
    """
    if name in failed_tools:
        return False, None, f"tool {name} already failed this round, skipped"
    handler = _TOOL_REGISTRY.get(name)
    if handler is None:
        # 未注册工具不算失败(可能是 LLM 编造的工具名),只返回提示
        return False, None, f"tool {name} not registered, available: {get_available_tools()}"
    try:
        # action_input 可能是 dict 或 str(LLM 输出格式不稳);统一成 kwargs
        if isinstance(action_input, str):
            try:
                action_input = json.loads(action_input) if action_input else {}
            except json.JSONDecodeError:
                action_input = {"query": action_input}  # 兜底:当 query 用
        if not isinstance(action_input, dict):
            action_input = {"input": action_input}
        result = await handler(**action_input)
        return True, result, ""
    except Exception as e:
        failed_tools.add(name)
        logger.warning("ReAct 工具 %s 调用失败: %s", name, e)
        return False, None, f"{type(e).__name__}: {e}"


def _format_observation(result: Any) -> tuple[str, Any]:
    """把工具原始返回转成 LLM 友好的字符串 + 保留 raw 给 trace。

    长结果截断到 2000 字符,避免吃光 token 预算。
    """
    try:
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        text = str(result)
    if len(text) > 2000:
        text = text[:2000] + "\n...(truncated)"
    return text, result


# =====================================================================
# Stuck 检测 - 使用共享 text_similarity 模块
# =====================================================================


def _jaccard_similarity(a: str, b: str) -> float:
    """Jaccard 相似度（代理到共享模块）。"""
    return _jaccard_sim(_tokenize(a), _tokenize(b))


# =====================================================================
# LLM 输出解析
# =====================================================================


_REACT_PROMPT_TEMPLATE = """你是身后事平台智能体。当前任务需要你使用 ReAct(Reasoning + Acting)模式逐步解决。

# 可用工具
{tools_description}

# ReAct 输出格式(必须严格遵循)
每一步必须输出 JSON,字段如下:
{{
  "thought": "你对当前情况的分析与下一步计划(1-2 句)",
  "action": "工具名,如 web_search / read_file;或 FINAL_ANSWER 表示已得到最终答案",
  "action_input": {{ "参数名": "参数值" }},
  "final_answer": "若 action=FINAL_ANSWER,这里填最终给用户的完整回答"
}}

# 终止条件
- 已得到足够信息回答用户 → action=FINAL_ANSWER
- 不确定时优先 FINAL_ANSWER + 诚实说明限制,不要无限调用工具

# 历史步骤
{history}

# 当前用户问题
{user_input}

# 下一步
请输出下一步 JSON(只输出 JSON,不要其他文本):"""


def _build_tools_description() -> str:
    """构建工具清单描述(给 LLM 看)"""
    tools = get_available_tools()
    if not tools:
        return "(无可用工具,请直接基于上下文作答,action=FINAL_ANSWER)"
    return "\n".join(f"- {name}" for name in tools)


def _parse_react_response(raw: str) -> dict[str, Any]:
    """解析 LLM 输出为 ReAct step dict。

    JSON 优先,失败时回退到文本提取(韧性优先,不抛异常)。
    """
    if not raw:
        return {"thought": "", "action": "NO_ACTION", "action_input": {}}

    # 1. 尝试提取 JSON(支持 ```json ... ``` 包裹)
    json_str = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", json_str, re.DOTALL)
    if fence_match:
        json_str = fence_match.group(1)
    else:
        # 找第一个 { 到最后一个 }
        first = json_str.find("{")
        last = json_str.rfind("}")
        if first != -1 and last != -1 and last > first:
            json_str = json_str[first : last + 1]

    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            # 规范化字段
            return {
                "thought": str(data.get("thought", "")),
                "action": str(data.get("action", "NO_ACTION")),
                "action_input": data.get("action_input", {}) or {},
                "final_answer": str(data.get("final_answer", "")),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. 回退:检测是否含 FINAL_ANSWER 标记
    if "FINAL_ANSWER" in raw:
        # 取 FINAL_ANSWER 之后的内容作为答案
        idx = raw.find("FINAL_ANSWER")
        answer = raw[idx + len("FINAL_ANSWER") :].strip().lstrip(":").strip()
        return {
            "thought": raw[: idx].strip(),
            "action": "FINAL_ANSWER",
            "action_input": {},
            "final_answer": answer or raw,
        }

    # 3. 完全无法解析 → 把整段当作 thought,action=NO_ACTION
    return {
        "thought": raw[:500],
        "action": "NO_ACTION",
        "action_input": {},
        "final_answer": "",
    }


# =====================================================================
# Self-Verification(可选)
# =====================================================================


_SELF_VERIFY_PROMPT = """请判断以下工具 Observation 是否符合 Thought 中的假设。
只输出 JSON: {{"passed": true|false, "reason": "简短说明"}}

Thought: {thought}
Action: {action}
Observation: {observation}"""


async def _self_verify(
    llm: LLMClient, thought: str, action: str, observation: str
) -> tuple[bool, str]:
    """LLM 自检 Observation 是否符合 Thought 假设。失败不阻断,默认通过。"""
    if not llm or not llm.api_key:
        return True, "llm_unavailable_skipped"
    try:
        resp = await llm.chat_json(
            [{"role": "user", "content": _SELF_VERIFY_PROMPT.format(
                thought=thought, action=action, observation=observation
            )}],
            temperature=0.0,
        )
        passed = bool(resp.get("passed", True))
        reason = str(resp.get("reason", ""))
        return passed, reason
    except Exception as e:
        logger.warning("ReAct self-verify 失败,默认通过: %s", e)
        return True, f"verify_error: {e}"


# =====================================================================
# ReAct 循环主体
# =====================================================================


class ReActLoop:
    """ReAct 循环 - 在 agent_node 内部封装 Thought→Action→Observation 迭代

    用法:
        loop = ReActLoop(llm=respond_llm, system_prompt=..., user_input=...)
        result = await loop.run()
        draft_response = result.final_answer
    """

    def __init__(
        self,
        llm: LLMClient | None,
        system_prompt: str,
        user_input: str,
        max_iterations: int = REACT_MAX_ITERATIONS,
        tool_budget: int = REACT_TOOL_BUDGET,
        iteration_max_tokens: int = REACT_ITERATION_MAX_TOKENS,
        self_verify: bool = REACT_SELF_VERIFY,
        trace_callback: Callable[[str, dict[str, Any]], None] | None = None,
        reflexion_engine: Any | None = None,
    ):
        self.llm = llm
        self.system_prompt = system_prompt
        self.user_input = user_input
        self.max_iterations = max(1, max_iterations)
        self.tool_budget = max(0, tool_budget)
        self.iteration_max_tokens = max(256, iteration_max_tokens)
        self.self_verify = self_verify
        self.trace_callback = trace_callback
        # P1.5: 可选注入 ReflexionEngine（仅 REACT_REFLEXION_ENABLED=1 时生效）
        self.reflexion_engine = reflexion_engine
        # P1.5: 反思历史（每轮反思的 reflection dict）
        self._reflections: list[dict[str, Any]] = []

        self._failed_tools: set[str] = set()
        self._tool_call_count = 0
        self._steps: list[ReActStep] = []
        self._total_tokens = 0

    def _emit_trace(self, name: str, attrs: dict[str, Any]) -> None:
        if self.trace_callback is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - trace 失败不影响业务
                self.trace_callback(name, attrs)

    def _build_history(self) -> str:
        """把已执行的 steps 渲染成 LLM 可读的历史"""
        if not self._steps:
            return "(无)"
        lines = []
        for s in self._steps:
            lines.append(
                f"[Step {s.iteration}]\n"
                f"  Thought: {s.thought}\n"
                f"  Action: {s.action}\n"
                f"  Action Input: {json.dumps(s.action_input, ensure_ascii=False, default=str)}\n"
                f"  Observation: {s.observation}"
            )
        return "\n\n".join(lines)

    async def _run_iteration(self, iteration: int) -> ReActStep:
        """执行一次 Thought → Action → Observation"""
        step = ReActStep(iteration=iteration)

        # === Thought 阶段 ===
        prompt = _REACT_PROMPT_TEMPLATE.format(
            tools_description=_build_tools_description(),
            history=self._build_history(),
            user_input=self.user_input,
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]

        if not self.llm or not self.llm.api_key:
            # LLM 不可用 → 直接终止
            step.thought = "LLM 不可用,无法执行 ReAct"
            step.action = "FINAL_ANSWER"
            step.tool_ok = False
            return step

        try:
            raw = await self.llm.chat(
                messages,
                temperature=0.2,
                max_tokens=self.iteration_max_tokens,
            )
            usage = self.llm.last_usage
            step.tokens_used = int(usage.get("total_tokens", 0))
            self._total_tokens += step.tokens_used
        except Exception as e:
            logger.warning("ReAct Thought LLM 调用失败: %s", e)
            step.thought = f"LLM 调用失败: {e}"
            step.action = "FINAL_ANSWER"
            step.tool_ok = False
            # LLM 异常 → 标记立即终止(由 run() 检测 tool_error 退出)
            step.tool_error = "llm_exception"
            return step

        parsed = _parse_react_response(raw)
        step.thought = parsed["thought"]
        step.action = parsed["action"]
        step.action_input = parsed["action_input"] if isinstance(
            parsed["action_input"], dict
        ) else {}

        self._emit_trace("react.thought", {
            "iteration": iteration,
            "thought": step.thought[:300],
            "action": step.action,
        })

        # === 终止判断 ===
        if step.action == "FINAL_ANSWER":
            step.observation = parsed.get("final_answer", "")
            return step

        # === tool budget 检查 ===
        if self._tool_call_count >= self.tool_budget:
            step.action = "FINAL_ANSWER"
            step.observation = "(工具 budget 已用尽,基于已有信息作答)"
            step.tool_ok = False
            step.tool_error = "tool_budget_exhausted"
            return step

        # === Action 阶段:分发工具 ===
        ok, result, err = await _dispatch_tool(
            step.action, step.action_input, self._failed_tools
        )
        step.tool_ok = ok
        step.tool_error = err
        if ok:
            self._tool_call_count += 1

        obs_text, obs_raw = _format_observation(result if ok else err)
        step.observation = obs_text
        step.observation_raw = obs_raw

        self._emit_trace("react.action", {
            "iteration": iteration,
            "action": step.action,
            "action_input": step.action_input,
            "tool_ok": ok,
        })
        self._emit_trace("react.observation", {
            "iteration": iteration,
            "observation": step.observation[:300],
        })

        # === Self-Verification(可选)===
        if self.self_verify and ok and self.llm and self.llm.api_key:
            passed, reason = await _self_verify(
                self.llm, step.thought, step.action, step.observation
            )
            step.self_verify_passed = passed
            if not passed:
                self._emit_trace("react.self_verify", {
                    "iteration": iteration,
                    "passed": False,
                    "reason": reason,
                })

        return step

    async def _summarize_history(self, reason: str) -> str:
        """max_iterations / stuck / tool_budget 到达后,综合历史作答"""
        if not self.llm or not self.llm.api_key:
            return ""
        prompt = (
            "你之前已通过 ReAct 模式执行了若干步骤。"
            f"现因 {reason} 终止,请基于历史步骤给出最终回答。\n"
            "若信息不足,诚实说明并建议咨询官方渠道。\n\n"
            f"用户问题: {self.user_input}\n\n"
            f"历史步骤:\n{self._build_history()}\n\n"
            "最终回答:"
        )
        try:
            return await self.llm.chat(
                [{"role": "system", "content": self.system_prompt},
                 {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=self.iteration_max_tokens,
            )
        except Exception as e:
            logger.warning("ReAct 历史综合作答失败: %s", e)
            return ""

    async def run(self) -> ReActResult:
        """执行完整 ReAct 循环,返回 ReActResult

        P1.5: 若 REACT_REFLEXION_ENABLED=1 且 reflexion_engine 注入,
        失败终止（stuck/self_verify_fail/max_iterations/error）时自动触发反思,
        注入反思上下文重试,最多 REACT_REFLEXION_MAX_ROUNDS 轮。
        默认关闭时行为完全不变（_run_core 原始逻辑）。
        """
        result = await self._run_core()

        # P1.5: Reflexion + ReAct 联动（feature flag 控制）
        if (
            REACT_REFLEXION_ENABLED
            and self.reflexion_engine is not None
            and not result.degraded
            and result.terminated_by in REACT_REFLEXION_TRIGGERS
        ):
            result = await self._run_with_reflexion(result)
        return result

    async def _run_core(self) -> ReActResult:
        """单次 ReAct 循环（原 run() 逻辑，P1.5 抽出供 reflexion 重试复用）"""
        result = ReActResult()

        if not self.llm or not self.llm.api_key:
            result.degraded = True
            result.note = "LLM 未配置,ReAct 降级"
            result.terminated_by = "llm_unavailable"
            return result

        prev_observation = ""
        for i in range(1, self.max_iterations + 1):
            step = await self._run_iteration(i)
            self._steps.append(step)
            result.total_tokens = self._total_tokens

            # 终止:LLM 异常(Thought 调用抛异常)→ 立即退出,不重试
            if step.tool_error == "llm_exception":
                result.terminated_by = "error"
                result.final_answer = ""
                result.steps = list(self._steps)
                return result

            # 终止:FINAL_ANSWER
            if step.action == "FINAL_ANSWER" and step.observation:
                result.final_answer = step.observation
                result.terminated_by = "final_answer"
                result.steps = list(self._steps)
                return result

            # 终止:Self-Verification 失败(可选,提前终止触发上层 Reflexion)
            if (
                self.self_verify
                and step.self_verify_passed is False
                and i >= 2  # 至少给一次重试机会
            ):
                result.terminated_by = "self_verify_fail"
                result.final_answer = await self._summarize_history("self-verify 失败")
                result.steps = list(self._steps)
                return result

            # 终止:Stuck 检测(连续 2 次 Observation 相似度 > 阈值)
            if (
                prev_observation
                and step.observation
                and _jaccard_similarity(prev_observation, step.observation)
                > REACT_STUCK_JACCARD_THRESHOLD
            ):
                logger.info("ReAct stuck 检测触发,iter=%d", i)
                self._emit_trace("react.stuck", {"iteration": i})
                result.terminated_by = "stuck"
                result.final_answer = await self._summarize_history("stuck 检测")
                result.steps = list(self._steps)
                return result

            prev_observation = step.observation

        # 终止:max_iterations
        result.terminated_by = "max_iterations"
        result.final_answer = await self._summarize_history("max_iterations 到达")
        result.steps = list(self._steps)
        return result

    # ==================================================================
    # P1.5: Reflexion + ReAct 联动
    # ==================================================================

    async def _run_with_reflexion(self, initial_result: ReActResult) -> ReActResult:
        """P1.5: 反思后重试 ReAct，最多 REACT_REFLEXION_MAX_ROUNDS 轮

        流程：
        1. 触发反思（调 ReflexionEngine._reflect）
        2. 注入反思上下文到 system_prompt，重置内部状态
        3. 重新跑 _run_core
        4. 若新结果非失败终止 → 返回新结果（合并 steps 历史）
        5. 否则继续反思重试，保留最佳结果
        """
        current = initial_result
        for round_num in range(1, REACT_REFLEXION_MAX_ROUNDS + 1):
            # 1. 触发反思
            reflection = await self._trigger_reflexion(current, round_num)
            if not reflection:
                # 反思失败 → 不再重试
                break
            self._reflections.append(reflection)
            self._emit_trace("react.reflexion", {
                "round": round_num,
                "failure_type": reflection.get("failure_type", ""),
                "adjustment_strategy": reflection.get("adjustment_strategy", ""),
            })

            # 2. 注入反思上下文到 system_prompt，重置内部状态
            original_prompt = self.system_prompt
            self.system_prompt = self._augment_prompt_with_reflection(reflection)
            self._failed_tools = set()
            self._tool_call_count = 0
            self._steps = []
            try:
                new_result = await self._run_core()
            finally:
                # 恢复原始 system_prompt（避免污染后续调用）
                self.system_prompt = original_prompt

            # 3. 若新结果非失败终止 → 合并 steps 历史，返回新结果
            if new_result.terminated_by not in REACT_REFLEXION_TRIGGERS:
                new_result.steps = list(current.steps) + list(new_result.steps)
                new_result.note = (
                    f"reflexion_round={round_num} succeeded "
                    f"({new_result.terminated_by})"
                )
                return new_result

            # 4. 否则继续反思重试，保留当前结果
            current = new_result

        # 所有轮次都失败 → 返回最后一次结果（已是失败终止）
        current.note = (
            f"reflexion_exhausted_rounds={REACT_REFLEXION_MAX_ROUNDS}"
        )
        return current

    async def _trigger_reflexion(
        self, result: ReActResult, round_num: int
    ) -> dict[str, Any] | None:
        """调 ReflexionEngine._reflect 反思当前失败

        ReflexionEngine 来自 deadman.reflexion.engine，其 _reflect 方法签名：
            async def _reflect(self, failure_info: dict, operation_type: str) -> dict
        返回 {failure_type, failure_reason, adjustment_strategy, adjusted_params}。
        反思失败返回 None（不阻断，外层直接用原结果）。
        """
        if self.reflexion_engine is None:
            return None
        failure_info = {
            "attempt": round_num,
            "failure_type": result.terminated_by,
            "failure_message": f"ReAct 终止: {result.terminated_by}",
            "input_summary": self.user_input[:200],
            "output_summary": (result.final_answer or "")[:200],
            "timestamp": "",  # ReflexionEngine 内部不依赖此字段
        }
        try:
            reflect = getattr(self.reflexion_engine, "_reflect", None)
            if reflect is None:
                return None
            reflection = await reflect(failure_info, "react")
            return reflection if isinstance(reflection, dict) else None
        except Exception as e:
            logger.warning("ReAct 触发 Reflexion 失败: %s", e)
            return None

    def _augment_prompt_with_reflection(self, reflection: dict[str, Any]) -> str:
        """把反思结果注入 system_prompt，避免重复犯错"""
        reason = reflection.get("failure_reason", "")
        strategy = reflection.get("adjustment_strategy", "")
        failure_type = reflection.get("failure_type", "")
        suffix = (
            "\n\n## 历史反思（避免重复犯错）\n"
            f"- 上次失败类型: {failure_type}\n"
            f"- 上次失败原因: {reason}\n"
            f"- 本次调整策略: {strategy}\n"
            "请基于反思调整推理与工具选择。\n"
        )
        return self.system_prompt + suffix


# =====================================================================
# 便捷入口 - 给 agent_node 用
# =====================================================================


async def run_react_loop(
    system_prompt: str,
    user_input: str,
    llm: LLMClient | None = None,
    trace_callback: Callable[[str, dict[str, Any]], None] | None = None,
    reflexion_engine: Any | None = None,
) -> ReActResult:
    """便捷入口:用 respond 用例 LLM 跑 ReAct 循环

    agent_node 调用此函数,把 system_prompt 和 user_input 传入,
    拿到 result.final_answer 作为 draft_response。

    P1.5: reflexion_engine 可选注入；仅当 DEADMAN_REACT_REFLEXION_ENABLED=1
    且 reflexion_engine 非 None 时，失败终止会自动触发反思重试。
    """
    if llm is None:
        llm = get_llm_for_use_case("respond")
    loop = ReActLoop(
        llm=llm,
        system_prompt=system_prompt,
        user_input=user_input,
        trace_callback=trace_callback,
        reflexion_engine=reflexion_engine,
    )
    return await loop.run()
