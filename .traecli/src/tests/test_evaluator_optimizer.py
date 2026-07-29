"""P1.3 Evaluator-Optimizer - 单元测试

覆盖：
- test_eval_optimize_loop: 评估循环
- test_eval_score_below_threshold_triggers_regenerate: 低于阈值触发重新生成
- test_eval_max_rounds_reached: 最大轮次到达
- test_eval_llm_unavailable_degraded: LLM 不可用降级

附加：
- test_eval_threshold_met_no_optimize: 首轮达标提前退出
- test_eval_evaluate_returns_zero_for_empty_answer: 空答案评分 0
"""

from __future__ import annotations

from typing import Any


from deadman.orchestration.evaluator_optimizer import (
    EVAL_OPTIMIZER_ENABLED,
    EVAL_OPTIMIZER_MAX_ROUNDS,
    EVAL_OPTIMIZER_THRESHOLD,
    EvaluatorOptimizer,
    OptimizationRound,
    OptimizeResult,
)


# =====================================================================
# Mock LLM Client
# =====================================================================


class MockLLMClient:
    """模拟 LLM - chat 返回队列，chat_json 返回队列或固定 dict"""

    def __init__(
        self,
        chat_responses: list[str] | None = None,
        chat_json_resp: dict[str, Any] | None = None,
        chat_json_responses: list[dict[str, Any]] | None = None,
        api_key: str = "mock-key",
    ):
        self.chat_responses = list(chat_responses) if chat_responses else []
        self.chat_json_resp = chat_json_resp or {"score": 0.9, "feedback": "ok"}
        self.chat_json_responses = list(chat_json_responses) if chat_json_responses else []
        self.api_key = api_key
        self.chat_count = 0
        self.chat_json_count = 0

    async def chat(self, messages, temperature=0.3, **kwargs):
        self.chat_count += 1
        if self.chat_responses:
            return self.chat_responses.pop(0)
        return "mock answer"

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        self.chat_json_count += 1
        if self.chat_json_responses:
            return self.chat_json_responses.pop(0)
        return dict(self.chat_json_resp)


# =====================================================================
# optimize - 评估循环
# =====================================================================


class TestEvalOptimizeLoop:
    async def test_eval_optimize_loop(self):
        """完整优化循环：生成 → 评估 → （达标）退出"""
        # 首轮生成 + 评估 score=0.9（达标），1 轮即退出
        llm = MockLLMClient(
            chat_responses=["首次答案"],
            chat_json_resp={"score": 0.9, "feedback": "好"},
        )
        eo = EvaluatorOptimizer(llm=llm, threshold=0.7)
        result = await eo.optimize("问题", context="上下文", max_rounds=3)

        assert result.degraded is False
        assert len(result.rounds) == 1
        assert result.rounds[0].answer == "首次答案"
        assert result.rounds[0].score == 0.9
        assert result.final_answer == "首次答案"
        assert result.final_score == 0.9

    async def test_eval_threshold_met_no_optimize(self):
        """首轮达标 → 不再重新生成，只跑 1 轮"""
        llm = MockLLMClient(
            chat_responses=["答案1", "答案2", "答案3"],  # 多准备，但应只用 1 个
            chat_json_resp={"score": 0.95, "feedback": "完美"},
        )
        eo = EvaluatorOptimizer(llm=llm, threshold=0.7)
        result = await eo.optimize("问题", max_rounds=3)

        assert len(result.rounds) == 1
        assert llm.chat_count == 1  # 只生成 1 次
        assert llm.chat_json_count == 1  # 只评估 1 次


# =====================================================================
# 低于阈值触发重新生成
# =====================================================================


class TestEvalScoreBelowThresholdTriggersRegenerate:
    async def test_eval_score_below_threshold_triggers_regenerate(self):
        """首轮 score=0.5 < 0.7 → 触发重新生成，第二轮达标退出"""
        # 生成顺序：首轮答案 → 优化后答案
        # 评估顺序：0.5（不达标）→ 0.85（达标）
        llm = MockLLMClient(
            chat_responses=["首次答案", "优化后答案"],
            chat_json_responses=[
                {"score": 0.5, "feedback": "不够完整"},
                {"score": 0.85, "feedback": "好"},
            ],
        )
        eo = EvaluatorOptimizer(llm=llm, threshold=0.7)
        result = await eo.optimize("问题", max_rounds=3)

        assert len(result.rounds) == 2
        # 第一轮：score=0.5，未达标
        assert result.rounds[0].answer == "首次答案"
        assert result.rounds[0].score == 0.5
        # 第二轮：score=0.85，达标
        assert result.rounds[1].answer == "优化后答案"
        assert result.rounds[1].score == 0.85
        # 最终返回最高分（0.85）
        assert result.final_answer == "优化后答案"
        assert result.final_score == 0.85

    async def test_eval_keeps_best_answer_when_later_rounds_worse(self):
        """后续轮次变差时，返回历史最佳答案"""
        # 三轮：0.6 → 0.4 → 0.5，均未达标（阈值 0.7）
        # 最终应返回最高分 0.6 对应的"答案1"
        llm = MockLLMClient(
            chat_responses=["答案1", "答案2", "答案3"],
            chat_json_responses=[
                {"score": 0.6, "feedback": "1"},
                {"score": 0.4, "feedback": "2"},
                {"score": 0.5, "feedback": "3"},
            ],
        )
        eo = EvaluatorOptimizer(llm=llm, threshold=0.7)
        result = await eo.optimize("问题", max_rounds=3)

        assert len(result.rounds) == 3
        # 最终返回最高分 0.6 对应答案
        assert result.final_answer == "答案1"
        assert result.final_score == 0.6


# =====================================================================
# 最大轮次到达
# =====================================================================


class TestEvalMaxRoundsReached:
    async def test_eval_max_rounds_reached(self):
        """所有轮次都未达标 → 跑满 max_rounds，返回最佳"""
        # 3 轮都 score=0.3，永不达标
        llm = MockLLMClient(
            chat_responses=["答案1", "答案2", "答案3"],
            chat_json_resp={"score": 0.3, "feedback": "差"},
        )
        eo = EvaluatorOptimizer(llm=llm, threshold=0.7)
        result = await eo.optimize("问题", max_rounds=3)

        assert len(result.rounds) == 3
        # 没有一轮达标
        for r in result.rounds:
            assert r.score < 0.7
        # 返回最佳（首轮 0.3，所有都 0.3，取第一个）
        assert result.final_answer == "答案1"
        assert result.final_score == 0.3

    async def test_eval_max_rounds_clamped_to_at_least_1(self):
        """max_rounds=0 → 钳到 1"""
        llm = MockLLMClient(
            chat_responses=["答案"],
            chat_json_resp={"score": 0.9, "feedback": "好"},
        )
        eo = EvaluatorOptimizer(llm=llm)
        result = await eo.optimize("问题", max_rounds=0)

        assert len(result.rounds) == 1


# =====================================================================
# LLM 不可用降级
# =====================================================================


class TestEvalLLMUnavailableDegraded:
    async def test_eval_llm_unavailable_degraded(self):
        """LLM=None → optimize 返回 degraded"""
        eo = EvaluatorOptimizer(llm=None)
        result = await eo.optimize("问题")
        assert result.degraded is True
        assert "降级" in result.note or "未配置" in result.note
        assert result.rounds == []

    async def test_eval_llm_no_api_key_degraded(self):
        """api_key 为空 → 返回 degraded"""
        llm = MockLLMClient(api_key="")
        eo = EvaluatorOptimizer(llm=llm)
        result = await eo.optimize("问题")
        assert result.degraded is True

    async def test_eval_generate_answer_llm_unavailable_empty(self):
        """LLM 不可用 → generate_answer 返回空"""
        eo = EvaluatorOptimizer(llm=None)
        answer = await eo.generate_answer("问题", "上下文")
        assert answer == ""

    async def test_eval_evaluate_returns_zero_for_empty_answer(self):
        """空答案 → evaluate 返回 (0.0, '答案为空')"""
        eo = EvaluatorOptimizer(llm=MockLLMClient())
        score, feedback = await eo.evaluate("", "问题")
        assert score == 0.0
        assert "空" in feedback

    async def test_eval_evaluate_exception_default_pass(self):
        """评估异常 → 默认 score=1.0（视为达标，避免无限循环）"""

        class FailingLLM(MockLLMClient):
            async def chat_json(self, messages, temperature=0.3, **kwargs):
                raise RuntimeError("eval error")
        llm = FailingLLM()
        eo = EvaluatorOptimizer(llm=llm)
        score, feedback = await eo.evaluate("答案", "问题")
        assert score == 1.0
        assert "eval_error" in feedback


# =====================================================================
# feature flag 默认关闭
# =====================================================================


class TestEvalOptimizerFeatureFlag:
    def test_feature_flag_default_disabled(self):
        """feature flag 默认关闭"""
        assert isinstance(EVAL_OPTIMIZER_ENABLED, bool)

    def test_threshold_in_range(self):
        assert 0.0 <= EVAL_OPTIMIZER_THRESHOLD <= 1.0

    def test_max_rounds_positive(self):
        assert EVAL_OPTIMIZER_MAX_ROUNDS >= 1

    def test_optimize_result_to_dict_serializable(self):
        """OptimizeResult.to_dict 可序列化"""
        import json
        result = OptimizeResult(
            final_answer="答案",
            final_score=0.8,
            rounds=[OptimizationRound(round_num=1, answer="答案", score=0.8)],
        )
        d = result.to_dict()
        json.dumps(d)
        assert d["final_answer"] == "答案"
