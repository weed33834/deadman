"""ReAct + Reflexion 联动单元测试 - 覆盖 P1.5 核心场景

场景设计:
1. feature flag 默认值与触发集合校验
2. Reflexion 关闭时不触发 _reflect（不破坏现有行为）
3. final_answer 正常终止不触发 Reflexion
4. degraded 结果不触发 Reflexion（LLM 不可用走降级路径）
5. reflexion_engine 为 None 不触发
6. ReAct 失败触发 Reflexion（max_iterations / stuck / error）
7. Reflexion 重试成功 - 注入反思后第二轮 final_answer
8. Reflexion 重试轮数限制 - 达到 max_rounds 仍失败
9. _reflect 返回 None / 非 dict / 抛异常 → 中断重试
10. 反思记忆注入到 system_prompt
11. 反思合并 steps 历史
12. reflexion trace span 发射
13. system_prompt 在 run() 后恢复
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import deadman.orchestration.react_loop as react_module
import pytest
from deadman.orchestration.react_loop import ReActLoop, ReActResult

# =====================================================================
# Mock LLM Client - 按 responses 队列依次返回,捕获所有 messages
# =====================================================================


class MockLLMClient:
    """模拟 LLM - 按 responses 队列依次返回,捕获所有 messages 供断言"""

    def __init__(
        self,
        responses: list[str] | None = None,
        api_key: str = "mock-key",
        raise_on_chat: bool = False,
    ):
        self.responses = list(responses) if responses else []
        self.api_key = api_key
        self.raise_on_chat = raise_on_chat
        self.call_count = 0
        self.last_usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        self.captured_messages: list[list[dict]] = []

    async def chat(self, messages, temperature=0.3, max_tokens=4096, **kwargs):
        self.call_count += 1
        self.captured_messages.append(messages)
        if self.raise_on_chat:
            raise RuntimeError("mock LLM error")
        if self.responses:
            return self.responses.pop(0)
        return ""

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        return {"passed": True, "reason": "ok"}


def _final_answer_response(answer: str) -> str:
    """构造 FINAL_ANSWER JSON 响应"""
    return json.dumps({
        "thought": "直接作答",
        "action": "FINAL_ANSWER",
        "action_input": {},
        "final_answer": answer,
    })


def _non_final_response(action: str = "web_search", query: str = "x") -> str:
    """构造非 FINAL_ANSWER 的 ReAct JSON 响应（调工具）"""
    return json.dumps({
        "thought": "继续搜索",
        "action": action,
        "action_input": {"query": query},
        "final_answer": "",
    })


def make_mock_reflexion_engine(
    reflection: dict[str, Any] | None = None,
    raise_on_reflect: bool = False,
    return_value: Any = ...,  # 默认用 reflection dict；传 None/sentinel 覆盖
) -> MagicMock:
    """构造 mock ReflexionEngine，_reflect 为 AsyncMock

    Args:
        reflection: 反思 dict（默认返回值）
        raise_on_reflect: True 时 _reflect 抛 RuntimeError
        return_value: 显式指定 _reflect 返回值（如 None / "not a dict"），
                      不传则用 reflection 参数
    """
    engine = MagicMock()
    if raise_on_reflect:
        engine._reflect = AsyncMock(side_effect=RuntimeError("reflect failed"))
    elif return_value is not ...:
        engine._reflect = AsyncMock(return_value=return_value)
    else:
        engine._reflect = AsyncMock(
            return_value=reflection or {
                "failure_type": "max_iterations",
                "failure_reason": "达到最大迭代次数",
                "adjustment_strategy": "简化推理直接作答",
                "adjusted_params": {},
            }
        )
    return engine


@pytest.fixture(autouse=True)
def _clear_tool_registry():
    """每个测试前后清空 ReAct 工具注册表，保证隔离"""
    react_module._TOOL_REGISTRY.clear()
    yield
    react_module._TOOL_REGISTRY.clear()


# =====================================================================
# Feature flag 与触发条件
# =====================================================================


class TestReflexionFeatureFlags:
    """P1.5 feature flag 默认值与触发集合"""

    def test_reflexion_flag_is_bool(self):
        # 默认应为 bool 类型（CI 可能显式启用，这里只验证类型）
        assert isinstance(react_module.REACT_REFLEXION_ENABLED, bool)

    def test_max_rounds_positive(self):
        assert react_module.REACT_REFLEXION_MAX_ROUNDS >= 1

    def test_triggers_set_contains_failure_types(self):
        triggers = react_module.REACT_REFLEXION_TRIGGERS
        # 失败终止类型应触发 Reflexion
        assert "stuck" in triggers
        assert "self_verify_fail" in triggers
        assert "max_iterations" in triggers
        assert "error" in triggers

    def test_final_answer_not_in_triggers(self):
        # 正常终止不触发
        assert "final_answer" not in react_module.REACT_REFLEXION_TRIGGERS

    def test_llm_unavailable_not_in_triggers(self):
        # 降级路径不触发（LLM 不可用时走降级，不需要反思）
        assert "llm_unavailable" not in react_module.REACT_REFLEXION_TRIGGERS


# =====================================================================
# Reflexion 不触发的场景
# =====================================================================


class TestReflexionNotTriggered:
    """验证各种不触发 Reflexion 的场景"""

    async def test_disabled_does_not_call_reflect(self, monkeypatch):
        # feature flag 关闭时，即使 engine 注入也不触发
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", False)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[_final_answer_response("答案")])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            reflexion_engine=engine,
        )
        result = await loop.run()

        assert result.terminated_by == "final_answer"
        engine._reflect.assert_not_called()

    async def test_final_answer_does_not_trigger(self, monkeypatch):
        # final_answer 正常终止不触发
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[_final_answer_response("答案")])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            reflexion_engine=engine,
        )
        result = await loop.run()

        assert result.terminated_by == "final_answer"
        engine._reflect.assert_not_called()

    async def test_degraded_does_not_trigger(self, monkeypatch):
        # LLM 不可用走降级路径，不触发 Reflexion
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        engine = make_mock_reflexion_engine()

        loop = ReActLoop(
            llm=None,
            system_prompt="sys",
            user_input="问题",
            reflexion_engine=engine,
        )
        result = await loop.run()

        assert result.degraded is True
        assert result.terminated_by == "llm_unavailable"
        engine._reflect.assert_not_called()

    async def test_no_engine_does_not_trigger(self, monkeypatch):
        # reflexion_engine=None 时即使 flag 开启也不触发
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)

        llm = MockLLMClient(responses=[
            "",  # iter1: NO_ACTION
            "综合:建议",  # summarize
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=None,
        )
        result = await loop.run()

        # 没有 engine → 不触发 Reflexion，直接返回 max_iterations 结果
        assert result.terminated_by == "max_iterations"


# =====================================================================
# Reflexion 触发与重试
# =====================================================================


class TestReflexionTriggered:
    """验证 Reflexion 被正确触发"""

    async def test_max_iterations_triggers_reflect(self, monkeypatch):
        # max_iterations 触发 _reflect
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 1)
        engine = make_mock_reflexion_engine()

        # 第一轮失败(max_iterations)，第二轮也失败 → 1 次 reflect
        llm = MockLLMClient(responses=[
            "",  # iter1 of first _run_core (NO_ACTION)
            "综合:建议",  # summarize of first _run_core
            "",  # iter1 of second _run_core (round 1)
            "综合:建议2",  # summarize of second _run_core
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        # _reflect 被调用 1 次
        engine._reflect.assert_called_once()
        # 最终仍失败(max_iterations)
        assert result.terminated_by == "max_iterations"

    async def test_reflect_called_with_correct_failure_info(self, monkeypatch):
        # _reflect 收到正确的 failure_info 与 operation_type
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 1)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "", "综合:建议", "", "综合:建议2",
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="如何注销户口",
            max_iterations=1,
            reflexion_engine=engine,
        )
        await loop.run()

        # 检查 _reflect 调用参数
        assert engine._reflect.call_count >= 1
        call_args = engine._reflect.call_args_list[0]
        failure_info = call_args.args[0]
        operation_type = call_args.args[1]

        assert operation_type == "react"
        assert failure_info["failure_type"] == "max_iterations"
        assert "max_iterations" in failure_info["failure_message"]
        assert "注销户口" in failure_info["input_summary"]
        assert failure_info["attempt"] == 1


class TestReflexionRetrySucceeds:
    """Reflexion 重试后成功"""

    async def test_retry_succeeds_on_round_1(self, monkeypatch):
        # 第一轮失败，反思后第二轮 final_answer
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "",  # iter1 of first _run_core (NO_ACTION)
            "综合:失败",  # summarize of first _run_core
            _final_answer_response("反思后成功"),  # iter1 of second _run_core
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        # 重试成功
        assert result.terminated_by == "final_answer"
        assert result.final_answer == "反思后成功"
        assert "reflexion_round=1 succeeded" in result.note
        # _reflect 调用 1 次
        engine._reflect.assert_called_once()

    async def test_retry_merges_steps_history(self, monkeypatch):
        # 成功后 steps 合并：失败轮 + 成功轮
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "", "综合:失败", _final_answer_response("成功"),
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        # 合并 steps：1 (失败轮) + 1 (成功轮) = 2
        assert len(result.steps) == 2
        # 第一步是失败轮的 NO_ACTION
        assert result.steps[0].action == "NO_ACTION"
        # 第二步是成功轮的 FINAL_ANSWER
        assert result.steps[1].action == "FINAL_ANSWER"


class TestReflexionRoundLimit:
    """Reflexion 重试轮数限制"""

    async def test_all_rounds_fail_returns_last(self, monkeypatch):
        # 所有轮次都失败 → 返回最后一次结果
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        # 3 次 _run_core (1 initial + 2 rounds)，每次都 max_iterations
        llm = MockLLMClient(responses=[
            "", "综合1",  # initial _run_core
            "", "综合2",  # round 1 _run_core
            "", "综合3",  # round 2 _run_core
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        # 最终仍失败
        assert result.terminated_by == "max_iterations"
        # _reflect 调用 2 次
        assert engine._reflect.call_count == 2
        # note 标记耗尽
        assert "reflexion_exhausted_rounds=2" in result.note

    async def test_round_attempt_incremented(self, monkeypatch):
        # 每轮 _reflect 收到的 attempt 递增
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "", "综合1", "", "综合2", "", "综合3",
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        await loop.run()

        # 第 1 次 reflect: attempt=1, 第 2 次: attempt=2
        assert engine._reflect.call_count == 2
        first_attempt = engine._reflect.call_args_list[0].args[0]["attempt"]
        second_attempt = engine._reflect.call_args_list[1].args[0]["attempt"]
        assert first_attempt == 1
        assert second_attempt == 2

    async def test_one_round_limit(self, monkeypatch):
        # max_rounds=1 → 只重试 1 次
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 1)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "", "综合1",  # initial
            "", "综合2",  # round 1
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        assert result.terminated_by == "max_iterations"
        assert engine._reflect.call_count == 1
        assert "reflexion_exhausted_rounds=1" in result.note


# =====================================================================
# Reflexion 降级路径
# =====================================================================


class TestReflexionDegradation:
    """Reflexion 反思失败时的降级"""

    async def test_reflect_returns_none_breaks(self, monkeypatch):
        # _reflect 返回 None → 中断重试，返回初始失败结果
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 3)
        engine = make_mock_reflexion_engine(return_value=None)

        llm = MockLLMClient(responses=[
            "", "综合:失败",  # initial _run_core
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        # _reflect 调用 1 次后返回 None → 中断
        engine._reflect.assert_called_once()
        # 返回初始失败结果
        assert result.terminated_by == "max_iterations"
        assert result.final_answer == "综合:失败"

    async def test_reflect_exception_breaks(self, monkeypatch):
        # _reflect 抛异常 → _trigger_reflexion 捕获返回 None → 中断
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 3)
        engine = make_mock_reflexion_engine(raise_on_reflect=True)

        llm = MockLLMClient(responses=["", "综合:失败"])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        # 异常被捕获，_reflect 调用 1 次后中断
        engine._reflect.assert_called_once()
        assert result.terminated_by == "max_iterations"

    async def test_reflect_non_dict_breaks(self, monkeypatch):
        # _reflect 返回非 dict（如字符串）→ 视为 None → 中断
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 3)
        engine = make_mock_reflexion_engine(return_value="not a dict")

        llm = MockLLMClient(responses=["", "综合:失败"])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        result = await loop.run()

        engine._reflect.assert_called_once()
        assert result.terminated_by == "max_iterations"


# =====================================================================
# 反思注入 system_prompt
# =====================================================================


class TestReflexionPromptAugmentation:
    """反思记忆注入到 system_prompt"""

    async def test_augmented_prompt_contains_reflection(self, monkeypatch):
        # 第二轮 _run_core 的 system_prompt 应包含反思信息
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)

        reflection = {
            "failure_type": "max_iterations",
            "failure_reason": "推理陷入循环",
            "adjustment_strategy": "直接作答",
            "adjusted_params": {},
        }
        engine = make_mock_reflexion_engine(reflection=reflection)

        llm = MockLLMClient(responses=[
            "", "综合:失败",  # first _run_core
            _final_answer_response("成功"),  # second _run_core
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="原始 system prompt",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        await loop.run()

        # captured_messages[0]: first _run_core iter1 (原始 prompt)
        # captured_messages[1]: first _run_core summarize (原始 prompt)
        # captured_messages[2]: second _run_core iter1 (augmented prompt)
        assert len(llm.captured_messages) >= 3
        second_run_system_msg = llm.captured_messages[2][0]["content"]
        assert "历史反思" in second_run_system_msg
        assert "推理陷入循环" in second_run_system_msg
        assert "直接作答" in second_run_system_msg
        # 原始 prompt 保留
        assert "原始 system prompt" in second_run_system_msg

    async def test_first_run_uses_original_prompt(self, monkeypatch):
        # 第一次 _run_core 应使用原始 system_prompt（未注入反思）
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "", "综合:失败", _final_answer_response("成功"),
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="原始 prompt",
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        await loop.run()

        # 第一次 _run_core 的 system msg 不含反思
        first_run_system_msg = llm.captured_messages[0][0]["content"]
        assert "历史反思" not in first_run_system_msg
        assert first_run_system_msg == "原始 prompt"

    async def test_prompt_restored_after_run(self, monkeypatch):
        # run() 完成后 system_prompt 恢复原始值
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 1)
        engine = make_mock_reflexion_engine()

        original_prompt = "原始 prompt"
        llm = MockLLMClient(responses=[
            "", "综合:失败", "", "综合:失败2",
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt=original_prompt,
            user_input="问题",
            max_iterations=1,
            reflexion_engine=engine,
        )
        await loop.run()

        # system_prompt 应恢复（finally 块保证）
        assert loop.system_prompt == original_prompt

    def test_augment_prompt_method_directly(self):
        # 直接测试 _augment_prompt_with_reflection 方法
        loop = ReActLoop(
            llm=None,
            system_prompt="base prompt",
            user_input="x",
        )
        reflection = {
            "failure_type": "stuck",
            "failure_reason": "陷入循环",
            "adjustment_strategy": "换工具",
            "adjusted_params": {},
        }
        augmented = loop._augment_prompt_with_reflection(reflection)

        assert "base prompt" in augmented
        assert "历史反思" in augmented
        assert "stuck" in augmented
        assert "陷入循环" in augmented
        assert "换工具" in augmented


# =====================================================================
# Trace span
# =====================================================================


class TestReflexionTraceSpan:
    """Reflexion trace span 发射"""

    async def test_reflexion_span_emitted(self, monkeypatch):
        # 反思触发时应发射 react.reflexion span
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[
            "", "综合:失败", _final_answer_response("成功"),
        ])
        spans: list[tuple[str, dict]] = []

        def cb(name, attrs):
            spans.append((name, attrs))

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            trace_callback=cb,
            reflexion_engine=engine,
        )
        await loop.run()

        # 应有 react.reflexion span
        reflexion_spans = [s for s in spans if s[0] == "react.reflexion"]
        assert len(reflexion_spans) == 1
        attrs = reflexion_spans[0][1]
        assert attrs["round"] == 1
        assert "failure_type" in attrs
        assert "adjustment_strategy" in attrs

    async def test_no_reflexion_span_when_disabled(self, monkeypatch):
        # 关闭时不应有 react.reflexion span
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", False)
        engine = make_mock_reflexion_engine()

        llm = MockLLMClient(responses=[_final_answer_response("答案")])
        spans: list[tuple[str, dict]] = []

        def cb(name, attrs):
            spans.append((name, attrs))

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            trace_callback=cb,
            reflexion_engine=engine,
        )
        await loop.run()

        reflexion_spans = [s for s in spans if s[0] == "react.reflexion"]
        assert len(reflexion_spans) == 0

    async def test_reflexion_span_emitted_per_round(self, monkeypatch):
        # 每轮反思都应发射 span
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 2)
        engine = make_mock_reflexion_engine()

        # 所有轮次都失败 → 2 个 span
        llm = MockLLMClient(responses=[
            "", "综合1", "", "综合2", "", "综合3",
        ])
        spans: list[tuple[str, dict]] = []

        def cb(name, attrs):
            spans.append((name, attrs))

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=1,
            trace_callback=cb,
            reflexion_engine=engine,
        )
        await loop.run()

        reflexion_spans = [s for s in spans if s[0] == "react.reflexion"]
        assert len(reflexion_spans) == 2
        assert reflexion_spans[0][1]["round"] == 1
        assert reflexion_spans[1][1]["round"] == 2


# =====================================================================
# 各触发类型覆盖
# =====================================================================


class TestReflexionTriggerTypes:
    """覆盖各种 terminated_by 触发 Reflexion"""

    async def test_stuck_triggers_reflexion(self, monkeypatch):
        # stuck 触发 Reflexion
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 1)
        engine = make_mock_reflexion_engine()

        # 注册工具，每次返回相同结果 → stuck
        async def same_tool(**_):
            return {"result": "same"}

        react_module._TOOL_REGISTRY["web_search"] = same_tool

        non_final = _non_final_response("web_search", "x")

        # 第一轮: iter1 + iter2 (stuck) + summarize
        # 第二轮 (round 1): 同样 stuck
        llm = MockLLMClient(responses=[
            non_final, non_final, "综合:stuck",
            non_final, non_final, "综合:stuck2",
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=5,
            reflexion_engine=engine,
        )
        await loop.run()

        # _reflect 被调用
        engine._reflect.assert_called()
        call_args = engine._reflect.call_args_list[0]
        assert call_args.args[0]["failure_type"] == "stuck"

    async def test_error_triggers_reflexion(self, monkeypatch):
        # error (LLM 异常) 触发 Reflexion
        monkeypatch.setattr(react_module, "REACT_REFLEXION_ENABLED", True)
        monkeypatch.setattr(react_module, "REACT_REFLEXION_MAX_ROUNDS", 1)
        engine = make_mock_reflexion_engine()

        # 每次 LLM chat 都抛异常 → terminated_by=error
        llm = MockLLMClient(raise_on_chat=True)

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=3,
            reflexion_engine=engine,
        )
        await loop.run()

        # _reflect 被调用
        engine._reflect.assert_called()
        call_args = engine._reflect.call_args_list[0]
        assert call_args.args[0]["failure_type"] == "error"


# =====================================================================
# ReActResult 数据结构
# =====================================================================


class TestReActResultSerialization:
    """ReActResult.to_dict() 可序列化（含 reflexion note）"""

    def test_to_dict_with_reflexion_note(self):
        result = ReActResult(
            final_answer="test",
            terminated_by="final_answer",
            note="reflexion_round=1 succeeded (final_answer)",
        )
        d = result.to_dict()
        assert d["note"] == "reflexion_round=1 succeeded (final_answer)"
        # 可 JSON 序列化
        json.dumps(d)

    def test_to_dict_with_exhausted_note(self):
        result = ReActResult(
            terminated_by="max_iterations",
            note="reflexion_exhausted_rounds=2",
        )
        d = result.to_dict()
        assert "exhausted" in d["note"]
