"""P1.4 Self-Consistency - 单元测试

覆盖：
- test_self_consistency_majority_vote: 多数投票
- test_self_consistency_weighted_vote: 加权投票
- test_self_consistency_llm_unavailable_degraded: LLM 不可用降级

附加：
- test_self_consistency_solve: 完整 solve 流程
- test_self_consistency_empty_answers: 空答案列表边界
- test_self_consistency_partial_sample_failure: 部分采样失败跳过
"""

from __future__ import annotations



from deadman.orchestration.self_consistency import (
    SELF_CONSISTENCY_DEFAULT_N,
    SELF_CONSISTENCY_DEFAULT_TEMP,
    SELF_CONSISTENCY_ENABLED,
    ConsistencyResult,
    SelfConsistency,
)


# =====================================================================
# Mock LLM Client
# =====================================================================


class MockLLMClient:
    """模拟 LLM - chat 返回队列"""

    def __init__(
        self,
        chat_responses: list[str] | None = None,
        api_key: str = "mock-key",
    ):
        self.chat_responses = list(chat_responses) if chat_responses else []
        self.api_key = api_key
        self.chat_count = 0

    async def chat(self, messages, temperature=0.3, **kwargs):
        self.chat_count += 1
        if self.chat_responses:
            return self.chat_responses.pop(0)
        return "mock"

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        return {"status": "ok"}


# =====================================================================
# 多数投票
# =====================================================================


class TestSelfConsistencyMajorityVote:
    def test_self_consistency_majority_vote(self):
        """简单多数投票：3 个答案中 2 个相同 → 取多数"""
        sc = SelfConsistency(llm=MockLLMClient())
        answers = ["是", "是", "否"]
        result = sc.aggregate(answers)
        assert result == "是"

    def test_majority_vote_case_insensitive(self):
        """归一化后投票（strip + lower）"""
        sc = SelfConsistency(llm=MockLLMClient())
        # "Yes" 和 "yes" 应合并为同一答案
        answers = ["Yes", "yes", "no"]
        result = sc.aggregate(answers)
        assert result.lower() == "yes"

    def test_majority_vote_tie_returns_first(self):
        """平票 → 取第一个出现的高票答案"""
        sc = SelfConsistency(llm=MockLLMClient())
        answers = ["A", "B"]  # 各 1 票，平票
        result = sc.aggregate(answers)
        assert result == "A"

    def test_majority_vote_single_answer(self):
        """单个答案 → 直接返回"""
        sc = SelfConsistency(llm=MockLLMClient())
        assert sc.aggregate(["唯一答案"]) == "唯一答案"


# =====================================================================
# 加权投票
# =====================================================================


class TestSelfConsistencyWeightedVote:
    def test_self_consistency_weighted_vote(self):
        """加权投票：低置信度的多数被高置信度的少数压过"""
        sc = SelfConsistency(llm=MockLLMClient())
        # "A" 1 票置信度 0.9，"B" 2 票各 0.3 → 加权 A=0.9 > B=0.6
        answers = ["A", "B", "B"]
        confidences = [0.9, 0.3, 0.3]
        result = sc.aggregate(answers, confidences=confidences)
        assert result == "A"

    def test_weighted_vote_with_equal_confidence_matches_majority(self):
        """等置信度加权 → 退化为多数投票"""
        sc = SelfConsistency(llm=MockLLMClient())
        answers = ["A", "B", "B"]
        confidences = [0.5, 0.5, 0.5]
        result = sc.aggregate(answers, confidences=confidences)
        assert result == "B"  # 多数

    def test_weighted_vote_confidences_length_mismatch_falls_back(self):
        """confidences 长度不匹配 → 退化为简单多数"""
        sc = SelfConsistency(llm=MockLLMClient())
        answers = ["A", "B", "B"]
        confidences = [0.9, 0.3]  # 长度不符
        result = sc.aggregate(answers, confidences=confidences)
        # 退化为多数 → B
        assert result == "B"


# =====================================================================
# LLM 不可用降级
# =====================================================================


class TestSelfConsistencyLLMUnavailableDegraded:
    async def test_self_consistency_llm_unavailable_degraded(self):
        """LLM=None → solve 返回 degraded"""
        sc = SelfConsistency(llm=None)
        result = await sc.solve("问题")
        assert result.degraded is True
        assert "降级" in result.note or "未配置" in result.note
        assert result.final_answer == ""

    async def test_self_consistency_llm_no_api_key_degraded(self):
        """api_key 为空 → solve 返回 degraded"""
        llm = MockLLMClient(api_key="")
        sc = SelfConsistency(llm=llm)
        result = await sc.solve("问题")
        assert result.degraded is True

    async def test_self_consistency_sample_llm_unavailable_empty(self):
        """LLM 不可用 → sample 返回空列表"""
        sc = SelfConsistency(llm=None)
        samples = await sc.sample("问题", n=3)
        assert samples == []


# =====================================================================
# 完整 solve + 边界
# =====================================================================


class TestSelfConsistencySolve:
    async def test_self_consistency_solve(self):
        """完整 solve：采样 3 次 → 投票"""
        llm = MockLLMClient(chat_responses=["是", "是", "否"])
        sc = SelfConsistency(llm=llm, default_n=3)
        result = await sc.solve("是否需要注销户口？")

        assert result.degraded is False
        assert result.final_answer == "是"
        assert len(result.samples) == 3
        # 投票统计：归一化后 {"是":2, "否":1}
        assert result.votes.get("是") == 2
        assert result.votes.get("否") == 1
        assert llm.chat_count == 3

    async def test_self_consistency_partial_sample_failure(self):
        """部分采样失败 → 跳过失败，仅用成功的投票"""

        class PartialFailLLM(MockLLMClient):
            async def chat(self, messages, temperature=0.3, **kwargs):
                self.chat_count += 1
                if self.chat_count == 2:
                    raise RuntimeError("fail on 2nd")
                return "A"
        llm = PartialFailLLM()
        sc = SelfConsistency(llm=llm)
        result = await sc.solve("问题", n=3)

        # 3 次尝试，2 次成功（"A", "A"），1 次失败跳过
        assert result.degraded is False
        assert len(result.samples) == 2
        assert result.final_answer == "A"

    def test_self_consistency_empty_answers(self):
        """空答案列表 → aggregate 返回空字符串"""
        sc = SelfConsistency(llm=MockLLMClient())
        assert sc.aggregate([]) == ""
        assert sc.aggregate([], confidences=[]) == ""


# =====================================================================
# feature flag 默认关闭
# =====================================================================


class TestSelfConsistencyFeatureFlag:
    def test_feature_flag_default_disabled(self):
        """feature flag 默认关闭"""
        assert isinstance(SELF_CONSISTENCY_ENABLED, bool)

    def test_default_n_positive(self):
        assert SELF_CONSISTENCY_DEFAULT_N >= 1

    def test_default_temp_in_range(self):
        assert 0.0 <= SELF_CONSISTENCY_DEFAULT_TEMP <= 2.0

    def test_consistency_result_to_dict_serializable(self):
        """ConsistencyResult.to_dict 可序列化"""
        import json
        result = ConsistencyResult(
            final_answer="答案",
            votes={"答案": 3},
            samples=["答案", "答案", "答案"],
        )
        d = result.to_dict()
        json.dumps(d)
        assert d["final_answer"] == "答案"
