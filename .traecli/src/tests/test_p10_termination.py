"""P10：可组合终止条件测试

借鉴 AutoGen TerminationCondition 设计，验证：
- 6 个具体终止条件类的独立行为
- `|` (OR) 短路组合
- `&` (AND) 全满足组合
- default_termination() 等价 P4 行为（向后兼容）
- 嵌套组合 A | B | C 和 (A | B) & C
- ExternalTermination 的 set/reset 状态
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from deadman.orchestration.state import create_initial_state
from deadman.orchestration.termination import (
    TerminationResult,
    MaxStepsTermination,
    StuckAgentTermination,
    TokenUsageTermination,
    MessageCountTermination,
    ExternalTermination,
    TextMentionTermination,
    default_termination,
)


# =====================================================================
# MaxStepsTermination
# =====================================================================


class TestMaxStepsTermination:
    def test_triggers_when_exceeds(self):
        term = MaxStepsTermination(max_steps=25)
        state = create_initial_state("x")
        state["step_count"] = 26
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "MaxStepsTermination"
        assert "26" in r.reason and "25" in r.reason

    def test_not_trigger_at_boundary(self):
        """step_count == max_steps 不触发（用 > 不是 >=）"""
        term = MaxStepsTermination(max_steps=25)
        state = create_initial_state("x")
        state["step_count"] = 25
        r = term.evaluate(state)
        assert r.should_terminate is False
        assert r.reason == ""

    def test_default_max_steps_is_25(self):
        """默认 max_steps=25，与 graph.MAX_STEPS 一致"""
        term = MaxStepsTermination()
        assert term.max_steps == 25

    def test_handles_missing_step_count(self):
        """state 没有 step_count 字段时不报错，按 0 处理"""
        term = MaxStepsTermination(max_steps=5)
        state = {"user_input": "x"}  # 无 step_count
        r = term.evaluate(state)
        assert r.should_terminate is False


# =====================================================================
# StuckAgentTermination
# =====================================================================


class TestStuckAgentTermination:
    def test_triggers_at_limit(self):
        term = StuckAgentTermination(repeat_limit=3)
        state = create_initial_state("x")
        state["stuck_count"] = 3
        state["last_agent_for_stuck"] = "legal_advisor"
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "StuckAgentTermination"
        assert "legal_advisor" in r.reason
        assert "3" in r.reason

    def test_not_trigger_below_limit(self):
        term = StuckAgentTermination(repeat_limit=3)
        state = create_initial_state("x")
        state["stuck_count"] = 2
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_default_repeat_limit_is_3(self):
        term = StuckAgentTermination()
        assert term.repeat_limit == 3


# =====================================================================
# TokenUsageTermination
# =====================================================================


class TestTokenUsageTermination:
    def test_triggers_when_total_exceeds(self):
        term = TokenUsageTermination(token_limit=50_000)
        state = {"metrics": {"token_usage": {"total_tokens": 60_000}}}
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "TokenUsageTermination"
        assert "60000" in r.reason

    def test_field_can_be_prompt_tokens(self):
        term = TokenUsageTermination(token_limit=10_000, field="prompt_tokens")
        state = {"metrics": {"token_usage": {"prompt_tokens": 12_000, "total_tokens": 99}}}
        r = term.evaluate(state)
        assert r.should_terminate is True

    def test_not_trigger_when_under_limit(self):
        term = TokenUsageTermination(token_limit=50_000)
        state = {"metrics": {"token_usage": {"total_tokens": 40_000}}}
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_handles_missing_token_usage(self):
        """metrics 里没有 token_usage 字段时按 0 处理"""
        term = TokenUsageTermination(token_limit=100)
        state = {"metrics": {}}
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_handles_missing_metrics(self):
        """state 没有 metrics 字段时不报错"""
        term = TokenUsageTermination(token_limit=100)
        state = {"user_input": "x"}
        r = term.evaluate(state)
        assert r.should_terminate is False


# =====================================================================
# MessageCountTermination
# =====================================================================


class TestMessageCountTermination:
    def test_triggers_at_max(self):
        term = MessageCountTermination(max_messages=5)
        state = {"agent_history": ["a", "b", "c", "d", "e"]}
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "MessageCountTermination"
        assert "5" in r.reason

    def test_not_trigger_below(self):
        term = MessageCountTermination(max_messages=5)
        state = {"agent_history": ["a", "b"]}
        r = term.evaluate(state)
        assert r.should_terminate is False


# =====================================================================
# ExternalTermination
# =====================================================================


class TestExternalTermination:
    def test_not_triggered_by_default(self):
        term = ExternalTermination()
        r = term.evaluate({})
        assert r.should_terminate is False

    def test_triggered_after_set(self):
        term = ExternalTermination()
        term.set()
        r = term.evaluate({})
        assert r.should_terminate is True
        assert r.source == "ExternalTermination"

    def test_reset_clears_flag(self):
        term = ExternalTermination()
        term.set()
        assert term.evaluate({}).should_terminate is True
        term.reset()
        assert term.evaluate({}).should_terminate is False


# =====================================================================
# TextMentionTermination
# =====================================================================


class TestTextMentionTermination:
    def test_triggers_when_keyword_present(self):
        term = TextMentionTermination(keyword="停止")
        state = {"user_input": "我想要停止对话"}
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "TextMentionTermination"
        assert "停止" in r.reason

    def test_not_trigger_when_keyword_absent(self):
        term = TextMentionTermination(keyword="停止")
        state = {"user_input": "继续帮我"}
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_custom_source_field(self):
        term = TextMentionTermination(keyword="ABORT", source_field="draft_response")
        state = {"draft_response": "operation ABORT now"}
        r = term.evaluate(state)
        assert r.should_terminate is True


# =====================================================================
# 组合条件 - OR / AND
# =====================================================================


class TestOrCombination:
    def test_or_triggers_when_left_true(self):
        """OR 短路：左侧终止时不评估右侧"""
        left = MaxStepsTermination(5)
        right = StuckAgentTermination(3)
        term = left | right
        state = create_initial_state("x")
        state["step_count"] = 10
        state["stuck_count"] = 0
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "MaxStepsTermination"

    def test_or_triggers_when_right_true(self):
        left = MaxStepsTermination(100)
        right = StuckAgentTermination(3)
        term = left | right
        state = create_initial_state("x")
        state["step_count"] = 1
        state["stuck_count"] = 5
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "StuckAgentTermination"

    def test_or_not_trigger_when_both_false(self):
        term = MaxStepsTermination(100) | StuckAgentTermination(10)
        state = create_initial_state("x")
        state["step_count"] = 1
        state["stuck_count"] = 1
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_or_chain_three_conditions(self):
        """A | B | C 嵌套组合"""
        term = (
            MaxStepsTermination(5)
            | StuckAgentTermination(3)
            | TokenUsageTermination(1000)
        )
        state = {"metrics": {"token_usage": {"total_tokens": 2000}}}
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "TokenUsageTermination"


class TestAndCombination:
    def test_and_triggers_when_both_true(self):
        term = MaxStepsTermination(50) & TokenUsageTermination(100_000)
        state = {"step_count": 51, "metrics": {"token_usage": {"total_tokens": 110_000}}}
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "And"
        assert "AND" in r.reason

    def test_and_not_trigger_when_only_one_true(self):
        term = MaxStepsTermination(50) & TokenUsageTermination(100_000)
        state = {"step_count": 51, "metrics": {"token_usage": {"total_tokens": 1000}}}
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_and_not_trigger_when_both_false(self):
        term = MaxStepsTermination(50) & TokenUsageTermination(100_000)
        state = {"step_count": 1, "metrics": {"token_usage": {"total_tokens": 100}}}
        r = term.evaluate(state)
        assert r.should_terminate is False


class TestNestedCombination:
    def test_or_then_and(self):
        """(A | B) & C：先 OR 再 AND"""
        term = (MaxStepsTermination(5) | StuckAgentTermination(3)) & TokenUsageTermination(1000)
        # A 触发但 C 未触发 → 不终止
        state = {"step_count": 10, "metrics": {"token_usage": {"total_tokens": 100}}}
        r = term.evaluate(state)
        assert r.should_terminate is False
        # A 触发且 C 触发 → 终止
        state2 = {"step_count": 10, "metrics": {"token_usage": {"total_tokens": 2000}}}
        r2 = term.evaluate(state2)
        assert r2.should_terminate is True

    def test_or_with_external(self):
        """默认 | ExternalTermination：外部信号可随时终止"""
        ext = ExternalTermination()
        term = MaxStepsTermination(100) | ext
        state = create_initial_state("x")
        # 未触发外部 → 不终止
        assert term.evaluate(state).should_terminate is False
        # 触发外部 → 终止
        ext.set()
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "ExternalTermination"


# =====================================================================
# default_termination 向后兼容
# =====================================================================


class TestDefaultTermination:
    def test_equals_p4_behavior(self):
        """default_termination() 等价 P4 的 MAX_STEPS + STUCK_AGENT_REPEAT_LIMIT"""
        term = default_termination()
        # step_count 超限触发
        state = create_initial_state("x")
        state["step_count"] = 26
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "MaxStepsTermination"

    def test_default_stuck_triggers(self):
        term = default_termination()
        state = create_initial_state("x")
        state["stuck_count"] = 3
        state["last_agent_for_stuck"] = "death_aftercare"
        r = term.evaluate(state)
        assert r.should_terminate is True
        assert r.source == "StuckAgentTermination"

    def test_default_not_triggered_normal(self):
        term = default_termination()
        state = create_initial_state("x")
        state["step_count"] = 5
        state["stuck_count"] = 1
        r = term.evaluate(state)
        assert r.should_terminate is False

    def test_default_is_or_combination(self):
        """default_termination 返回的是 OR 组合对象"""
        term = default_termination()
        # 验证 repr 含 | 符号
        assert "|" in repr(term)


# =====================================================================
# TerminationResult 不可变性
# =====================================================================


class TestTerminationResult:
    def test_frozen_dataclass(self):
        """frozen=True，字段不可修改"""
        r = TerminationResult(True, "reason", "source")
        with pytest.raises(FrozenInstanceError):
            r.should_terminate = False  # type: ignore

    def test_equality(self):
        """可 == 断言"""
        r1 = TerminationResult(True, "max_steps:26>25", "MaxStepsTermination")
        r2 = TerminationResult(True, "max_steps:26>25", "MaxStepsTermination")
        assert r1 == r2

    def test_default_values(self):
        r = TerminationResult(False)
        assert r.reason == ""
        assert r.source == ""


# =====================================================================
# _is_stuck 集成（P10 委托给 default_termination）
# =====================================================================


class TestIsStuckDelegation:
    """验证 graph._is_stuck 仍可用，行为等价 P4"""

    def test_is_stuck_still_callable(self):
        from deadman.orchestration.graph import _is_stuck

        state = create_initial_state("x")
        state["step_count"] = 26
        stuck, reason = _is_stuck(state)
        assert stuck is True
        assert "max_steps" in reason

    def test_is_stuck_returns_tuple(self):
        """_is_stuck 返回 (bool, str) 元组，向后兼容"""
        from deadman.orchestration.graph import _is_stuck

        state = create_initial_state("x")
        result = _is_stuck(state)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)
