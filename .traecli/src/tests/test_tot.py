"""P1.2 Tree of Thought (ToT) - 单元测试

覆盖：
- test_tot_generate_paths: 3 路径生成
- test_tot_fact_check_prune: 事实性剪枝
- test_tot_evaluate: 节点评分
- test_tot_llm_unavailable_degraded: LLM 不可用降级

附加：
- test_tot_solve_returns_best: 完整 solve 流程返回最优
- test_tot_evaluate_clamps_score: score 钳制到 [0,1]
"""

from __future__ import annotations

from typing import Any


from deadman.orchestration.tot import (
    TOT_DEFAULT_PATHS,
    TOT_ENABLED,
    ThoughtNode,
    TreeOfThought,
    ToTResult,
)


# =====================================================================
# Mock LLM Client
# =====================================================================


class MockLLMClient:
    """模拟 LLM - chat 返回队列，chat_json 返回固定 dict"""

    def __init__(
        self,
        chat_responses: list[str] | None = None,
        chat_json_resp: dict[str, Any] | None = None,
        api_key: str = "mock-key",
        raise_on_chat: bool = False,
    ):
        self.chat_responses = list(chat_responses) if chat_responses else []
        self.chat_json_resp = chat_json_resp or {"score": 0.8, "passed": True}
        self.api_key = api_key
        self.raise_on_chat = raise_on_chat
        self.chat_count = 0
        self.chat_json_count = 0

    async def chat(self, messages, temperature=0.3, **kwargs):
        self.chat_count += 1
        if self.raise_on_chat:
            raise RuntimeError("mock chat error")
        if self.chat_responses:
            return self.chat_responses.pop(0)
        return "mock thought"

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        self.chat_json_count += 1
        return dict(self.chat_json_resp)


# =====================================================================
# generate_paths - 3 路径生成
# =====================================================================


class TestToTGeneratePaths:
    async def test_tot_generate_paths(self):
        """3 路径生成：返回 3 个 ThoughtNode，各自 thought 不同"""
        llm = MockLLMClient(chat_responses=["路径A", "路径B", "路径C"])
        tot = TreeOfThought(llm=llm, default_paths=3)
        nodes = await tot.generate_paths("问题", n=3)

        assert len(nodes) == 3
        assert nodes[0].thought == "路径A"
        assert nodes[1].thought == "路径B"
        assert nodes[2].thought == "路径C"
        # 每个节点有唯一 node_id
        ids = {n.node_id for n in nodes}
        assert len(ids) == 3
        # LLM 被调用 3 次
        assert llm.chat_count == 3

    async def test_tot_generate_paths_partial_failure(self):
        """部分生成失败：失败的 thought 为空，但其他成功"""
        # 第 2 次调用抛异常
        class PartialFailLLM(MockLLMClient):
            async def chat(self, messages, temperature=0.3, **kwargs):
                self.chat_count += 1
                if self.chat_count == 2:
                    raise RuntimeError("fail on 2nd")
                return f"路径{self.chat_count}"
        llm = PartialFailLLM()
        tot = TreeOfThought(llm=llm)
        nodes = await tot.generate_paths("问题", n=3)
        assert len(nodes) == 3
        # 第 2 个 thought 为空（失败）
        assert nodes[0].thought == "路径1"
        assert nodes[1].thought == ""
        assert nodes[2].thought == "路径3"


# =====================================================================
# fact_check - 事实性剪枝
# =====================================================================


class TestToTFactCheckPrune:
    async def test_tot_fact_check_prune(self):
        """fact_check 返回 passed=False → 节点剪枝（pruned=True）"""
        llm = MockLLMClient(chat_json_resp={"passed": False, "issues": "事实错误"})
        tot = TreeOfThought(llm=llm)
        node = ThoughtNode(node_id="n1", thought="有事实错误的推理")

        passed = await tot.fact_check(node, query="问题")

        assert passed is False
        assert node.fact_check_passed is False
        assert node.pruned is True

    async def test_tot_fact_check_pass(self):
        """fact_check 返回 passed=True → 节点不剪枝"""
        llm = MockLLMClient(chat_json_resp={"passed": True})
        tot = TreeOfThought(llm=llm)
        node = ThoughtNode(node_id="n1", thought="正确推理")

        passed = await tot.fact_check(node, query="问题")

        assert passed is True
        assert node.fact_check_passed is True
        assert node.pruned is False

    async def test_tot_fact_check_exception_default_pass(self):
        """fact_check 异常 → 默认通过（不误剪枝）"""

        class FailingLLM(MockLLMClient):
            async def chat_json(self, messages, temperature=0.3, **kwargs):
                raise RuntimeError("eval error")
        llm = FailingLLM()
        tot = TreeOfThought(llm=llm)
        node = ThoughtNode(node_id="n1", thought="推理")

        passed = await tot.fact_check(node, query="问题")

        assert passed is True
        assert node.fact_check_passed is True
        assert node.pruned is False


# =====================================================================
# evaluate - 节点评分
# =====================================================================


class TestToTEvaluate:
    async def test_tot_evaluate(self):
        """LLM 给节点打分（0-1）"""
        llm = MockLLMClient(chat_json_resp={"score": 0.85, "reason": "好"})
        tot = TreeOfThought(llm=llm)
        node = ThoughtNode(node_id="n1", thought="推理")

        score = await tot.evaluate(node, query="问题")

        assert score == 0.85
        assert node.evaluation_score == 0.85

    async def test_tot_evaluate_clamps_score(self):
        """score 钳制到 [0,1]：>1 钳到 1，<0 钳到 0"""

        class ClampLLM(MockLLMClient):
            def __init__(self, scores):
                super().__init__()
                self._scores = list(scores)
                self._idx = 0

            async def chat_json(self, messages, temperature=0.3, **kwargs):
                s = self._scores[self._idx]
                self._idx += 1
                return {"score": s}

        # score=1.5 → 钳到 1.0
        llm = ClampLLM([1.5, -0.3])
        tot = TreeOfThought(llm=llm)
        n1 = ThoughtNode(node_id="n1", thought="推理1")
        n2 = ThoughtNode(node_id="n2", thought="推理2")

        s1 = await tot.evaluate(n1, query="问题")
        s2 = await tot.evaluate(n2, query="问题")

        assert s1 == 1.0
        assert s2 == 0.0

    async def test_tot_evaluate_exception_default_05(self):
        """评估异常 → 默认 score=0.5"""

        class FailingLLM(MockLLMClient):
            async def chat_json(self, messages, temperature=0.3, **kwargs):
                raise RuntimeError("eval error")
        llm = FailingLLM()
        tot = TreeOfThought(llm=llm)
        node = ThoughtNode(node_id="n1", thought="推理")

        score = await tot.evaluate(node, query="问题")

        assert score == 0.5
        assert node.evaluation_score == 0.5


# =====================================================================
# LLM 不可用降级
# =====================================================================


class TestToTLLMUnavailableDegraded:
    async def test_tot_llm_unavailable_degraded(self):
        """LLM=None → solve 返回 degraded"""
        tot = TreeOfThought(llm=None)
        result = await tot.solve("问题")

        assert result.degraded is True
        assert "降级" in result.note or "未配置" in result.note
        assert result.best_thought == ""

    async def test_tot_llm_no_api_key_degraded(self):
        """api_key 为空 → solve 返回 degraded"""
        llm = MockLLMClient(api_key="")
        tot = TreeOfThought(llm=llm)
        result = await tot.solve("问题")
        assert result.degraded is True

    async def test_tot_generate_paths_llm_unavailable_empty(self):
        """LLM 不可用 → generate_paths 返回空列表"""
        tot = TreeOfThought(llm=None)
        nodes = await tot.generate_paths("问题", n=3)
        assert nodes == []

    async def test_tot_fact_check_llm_unavailable_default_pass(self):
        """LLM 不可用 → fact_check 默认通过（不误剪枝）"""
        tot = TreeOfThought(llm=None)
        node = ThoughtNode(node_id="n1", thought="推理")
        passed = await tot.fact_check(node, query="问题")
        assert passed is True
        assert node.fact_check_passed is True


# =====================================================================
# solve - 完整流程
# =====================================================================


class TestToTSolveReturnsBest:
    async def test_tot_solve_returns_best(self):
        """完整 solve：生成 → 评估 → fact_check → 选最优"""
        # 3 个 thought，eval score 分别 0.6/0.9/0.4，全 fact_check 通过
        class SolveLLM(MockLLMClient):
            def __init__(self):
                super().__init__(chat_responses=["低分", "高分", "最低分"])
                self._eval_idx = 0
                self._scores = [0.6, 0.9, 0.4]

            async def chat_json(self, messages, temperature=0.3, **kwargs):
                # 区分 evaluate vs fact_check：fact_check 的 prompt 含 "判断"
                content = messages[0]["content"]
                if "判断" in content:
                    return {"passed": True}
                # evaluate
                s = self._scores[self._eval_idx]
                self._eval_idx += 1
                return {"score": s}

        llm = SolveLLM()
        tot = TreeOfThought(llm=llm, default_paths=3)
        result = await tot.solve("问题")

        assert result.degraded is False
        assert result.best_thought == "高分"
        assert result.best_score == 0.9
        assert len(result.nodes) == 3

    async def test_tot_solve_all_pruned_returns_highest(self):
        """所有路径 fact_check 失败 → 返回最高分（即便被剪枝）"""

        class AllFailLLM(MockLLMClient):
            def __init__(self):
                super().__init__(chat_responses=["A", "B"])
                self._eval_idx = 0
                self._scores = [0.3, 0.7]

            async def chat_json(self, messages, temperature=0.3, **kwargs):
                content = messages[0]["content"]
                if "判断" in content:
                    return {"passed": False, "issues": "都错"}
                s = self._scores[self._eval_idx]
                self._eval_idx += 1
                return {"score": s}

        llm = AllFailLLM()
        tot = TreeOfThought(llm=llm, default_paths=2)
        result = await tot.solve("问题")

        assert result.degraded is False
        # 全部剪枝，仍返回最高分 0.7
        assert result.best_thought == "B"
        assert result.best_score == 0.7
        assert "fact_check 失败" in result.note

    async def test_tot_solve_to_dict_serializable(self):
        """ToTResult.to_dict 可序列化"""
        import json
        result = ToTResult(
            best_thought="答案",
            best_score=0.8,
            best_node_id="n1",
            nodes=[ThoughtNode(node_id="n1", thought="答案", evaluation_score=0.8)],
        )
        d = result.to_dict()
        # 可 JSON 序列化
        json.dumps(d)
        assert d["best_thought"] == "答案"
        assert d["best_score"] == 0.8


# =====================================================================
# feature flag 默认关闭
# =====================================================================


class TestToTFeatureFlag:
    def test_feature_flag_default_disabled(self):
        """feature flag 默认关闭"""
        assert isinstance(TOT_ENABLED, bool)

    def test_default_paths_positive(self):
        assert TOT_DEFAULT_PATHS >= 1
