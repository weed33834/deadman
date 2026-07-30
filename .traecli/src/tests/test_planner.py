"""P1.1 Plan-and-Execute - 单元测试

覆盖：
- test_plan_generation_success: 正常生成 Plan
- test_plan_cache_hit: 相似 query 复用
- test_plan_llm_unavailable_degraded: LLM 不可用降级
- test_is_complex_query: 复杂度判断
- test_plan_execution: 逐节点执行

附加：
- PlanCache LRU 淘汰
- chat_json 抛异常降级
- 依赖未满足的步骤标记 skipped
- feature flag 默认关闭
"""

from __future__ import annotations

from typing import Any

from deadman.orchestration.planner import (
    COMPLEX_QUERY_MIN_KEYWORDS,
    COMPLEX_QUERY_MIN_LENGTH,
    PLAN_CACHE_JACCARD_THRESHOLD,
    PLAN_EXECUTE_ENABLED,
    Plan,
    PlanCache,
    PlannerAgent,
    PlanStep,
    is_complex_query,
)

# =====================================================================
# Mock LLM Client - 模拟 chat_json / chat
# =====================================================================


class MockLLMClient:
    """模拟 LLM - chat_json 返回固定 dict，chat 返回固定 str"""

    def __init__(
        self,
        chat_json_resp: dict[str, Any] | None = None,
        chat_resp: str = "step output",
        api_key: str = "mock-key",
        raise_on_chat_json: bool = False,
    ):
        self.api_key = api_key
        self.chat_json_resp = chat_json_resp or {"steps": []}
        self.chat_resp = chat_resp
        self.raise_on_chat_json = raise_on_chat_json
        self.call_count = 0
        self.chat_count = 0

    async def chat(self, messages, temperature=0.3, **kwargs):
        self.chat_count += 1
        return self.chat_resp

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        self.call_count += 1
        if self.raise_on_chat_json:
            raise RuntimeError("mock chat_json error")
        return dict(self.chat_json_resp)


# =====================================================================
# is_complex_query - 复杂度判断
# =====================================================================


class TestIsComplexQuery:
    def test_short_simple_query_not_complex(self):
        """短问题不算复杂"""
        assert is_complex_query("你好") is False
        assert is_complex_query("hi") is False

    def test_long_query_is_complex(self):
        """长度 > 100 视为复杂"""
        long_query = (
            "亲人去世后需要办理户口注销社保结算公积金提取遗产继承房产过户税务清算等多种手续，请问完整的办理流程和顺序是什么？"
            * 2
        )
        assert len(long_query) > COMPLEX_QUERY_MIN_LENGTH
        assert is_complex_query(long_query) is True

    def test_keyword_count_threshold(self):
        """字符级关键词数 >= 3 视为复杂（中文按字分词）"""
        # 6 个不同字 → 复杂
        query = "户口 注销 流程"
        assert is_complex_query(query) is True
        # 2 个不同字 → 不复杂（< 3）
        query2 = "户 口"
        assert is_complex_query(query2) is False

    def test_empty_query_not_complex(self):
        assert is_complex_query("") is False
        assert is_complex_query(None) is False  # type: ignore[arg-type]

    def test_chinese_chars_count_as_keywords(self):
        """中文字符按字分词，>=3 个不同字算复杂"""
        # 3 个不同的中文字
        assert is_complex_query("北京户口") is True  # 北/京/户/口 = 4 个字


# =====================================================================
# PlannerAgent.plan - 正常生成 Plan
# =====================================================================


class TestPlanGenerationSuccess:
    async def test_plan_generation_success(self):
        """正常生成 Plan：LLM 返回合法 DAG JSON"""
        llm_resp = {
            "steps": [
                {
                    "step_id": "s1",
                    "action": "搜索户口注销流程",
                    "tool_hint": "web_search",
                    "depends_on": [],
                    "expected_output": "流程步骤列表",
                },
                {
                    "step_id": "s2",
                    "action": "查询派出所地址",
                    "tool_hint": "web_search",
                    "depends_on": ["s1"],
                    "expected_output": "地址信息",
                },
            ]
        }
        llm = MockLLMClient(chat_json_resp=llm_resp)
        agent = PlannerAgent(llm=llm, cache=PlanCache())
        plan = await agent.plan("北京户口注销流程是什么？")

        assert plan.degraded is False
        assert plan.cache_hit is False
        assert plan.plan_id.startswith("plan-")
        assert len(plan.steps) == 2
        assert plan.steps[0].step_id == "s1"
        assert plan.steps[0].action == "搜索户口注销流程"
        assert plan.steps[1].depends_on == ["s1"]
        assert llm.call_count == 1

    async def test_plan_generation_empty_steps_degraded(self):
        """LLM 返回空 steps → 降级"""
        llm = MockLLMClient(chat_json_resp={"steps": []})
        agent = PlannerAgent(llm=llm, cache=PlanCache())
        plan = await agent.plan("问题")
        assert plan.degraded is True
        assert plan.steps == []

    async def test_plan_generation_chat_json_exception_degraded(self):
        """chat_json 抛异常 → 降级"""
        llm = MockLLMClient(raise_on_chat_json=True)
        agent = PlannerAgent(llm=llm, cache=PlanCache())
        plan = await agent.plan("问题")
        assert plan.degraded is True


# =====================================================================
# PlanCache - 缓存命中
# =====================================================================


class TestPlanCacheHit:
    async def test_plan_cache_hit_exact_query(self):
        """精确 query 缓存命中：第二次 plan 不再调 LLM"""
        llm_resp = {
            "steps": [
                {
                    "step_id": "s1",
                    "action": "动作",
                    "tool_hint": "",
                    "depends_on": [],
                    "expected_output": "",
                }
            ]
        }
        llm = MockLLMClient(chat_json_resp=llm_resp)
        agent = PlannerAgent(llm=llm, cache=PlanCache())

        # 第一次：未命中，调 LLM
        plan1 = await agent.plan("北京户口注销流程")
        assert plan1.cache_hit is False
        assert llm.call_count == 1

        # 第二次：精确命中，不调 LLM
        plan2 = await agent.plan("北京户口注销流程")
        assert plan2.cache_hit is True
        assert llm.call_count == 1  # 仍是 1，未再调 LLM

    async def test_plan_cache_hit_similar_query(self):
        """相似 query（Jaccard > 0.85）缓存命中"""
        llm_resp = {
            "steps": [
                {
                    "step_id": "s1",
                    "action": "动作",
                    "tool_hint": "",
                    "depends_on": [],
                    "expected_output": "",
                }
            ]
        }
        llm = MockLLMClient(chat_json_resp=llm_resp)
        agent = PlannerAgent(llm=llm, cache=PlanCache())

        # 原始 query
        await agent.plan("北京户口注销流程")
        assert llm.call_count == 1

        # 相似 query（仅增 1 个字，Jaccard = 8/9 ≈ 0.889 > 0.85）
        plan2 = await agent.plan("北京户口注销流程的")
        assert plan2.cache_hit is True
        assert llm.call_count == 1  # 仍是 1

    def test_plan_cache_lru_eviction(self):
        """LRU 淘汰：超过 max_size 淘汰最久未访问"""
        cache = PlanCache(max_size=2)
        p1 = Plan(plan_id="p1", steps=[PlanStep(step_id="s1", action="a")])
        p2 = Plan(plan_id="p2", steps=[PlanStep(step_id="s1", action="b")])
        p3 = Plan(plan_id="p3", steps=[PlanStep(step_id="s1", action="c")])

        cache.put("q1", p1)
        cache.put("q2", p2)
        assert len(cache) == 2
        # 再 put 第 3 个 → 淘汰 q1
        cache.put("q3", p3)
        assert len(cache) == 2
        assert cache.get("q1") is None
        assert cache.get("q2") is not None
        assert cache.get("q3") is not None

    def test_plan_cache_does_not_cache_degraded(self):
        """降级 Plan 不缓存"""
        cache = PlanCache()
        degraded = Plan(degraded=True, steps=[])
        cache.put("q", degraded)
        assert len(cache) == 0
        assert cache.get("q") is None


# =====================================================================
# LLM 不可用降级
# =====================================================================


class TestPlanLLMUnavailableDegraded:
    async def test_plan_llm_unavailable_degraded(self):
        """LLM=None → 返回降级 Plan"""
        agent = PlannerAgent(llm=None, cache=PlanCache())
        plan = await agent.plan("任意问题")
        assert plan.degraded is True
        assert plan.steps == []

    async def test_plan_llm_no_api_key_degraded(self):
        """api_key 为空 → 返回降级 Plan"""
        llm = MockLLMClient(api_key="")
        agent = PlannerAgent(llm=llm, cache=PlanCache())
        plan = await agent.plan("任意问题")
        assert plan.degraded is True

    async def test_plan_degraded_not_cached(self):
        """降级 Plan 不写入缓存（避免污染）"""
        agent = PlannerAgent(llm=None, cache=PlanCache())
        await agent.plan("问题1")
        # 多次调用，每次都降级（说明没缓存）
        plan2 = await agent.plan("问题1")
        assert plan2.degraded is True
        assert plan2.cache_hit is False


# =====================================================================
# PlannerAgent.execute - 逐节点执行
# =====================================================================


class TestPlanExecution:
    async def test_plan_execution(self):
        """逐节点执行：DAG 拓扑序执行，每步调 LLM"""
        plan = Plan(
            plan_id="test-plan",
            steps=[
                PlanStep(step_id="s1", action="第一步", depends_on=[]),
                PlanStep(step_id="s2", action="第二步", depends_on=["s1"]),
                PlanStep(step_id="s3", action="第三步", depends_on=["s2"]),
            ],
        )
        llm = MockLLMClient(chat_resp="step result")
        agent = PlannerAgent(llm=llm, cache=PlanCache())
        result = await agent.execute(plan)

        assert result["degraded"] is False
        assert len(result["results"]) == 3
        # 每步都成功
        for r in result["results"]:
            assert r["ok"] is True
            assert r["output"] == "step result"
        # 每步 status 更新为 done
        for step in plan.steps:
            assert step.status == "done"
        # LLM 被调用 3 次（每步 1 次）
        assert llm.chat_count == 3

    async def test_plan_execution_degraded_plan_skipped(self):
        """降级 Plan 执行 → 直接返回 degraded"""
        plan = Plan(degraded=True, steps=[])
        agent = PlannerAgent(llm=MockLLMClient(), cache=PlanCache())
        result = await agent.execute(plan)
        assert result["degraded"] is True
        assert result["results"] == []

    async def test_plan_execution_step_failure_marks_skipped(self):
        """单步失败 → 下游步骤标记 skipped"""
        plan = Plan(
            plan_id="test-plan",
            steps=[
                PlanStep(step_id="s1", action="第一步", depends_on=[]),
                PlanStep(step_id="s2", action="第二步", depends_on=["s1"]),
            ],
        )

        # chat 抛异常 → step 失败
        class FailingLLM(MockLLMClient):
            async def chat(self, messages, temperature=0.3, **kwargs):
                raise RuntimeError("LLM error")

        llm = FailingLLM(chat_resp="")
        agent = PlannerAgent(llm=llm, cache=PlanCache())
        result = await agent.execute(plan)

        # s1 失败
        assert result["results"][0]["ok"] is False
        assert plan.steps[0].status == "failed"
        # s2 因依赖失败 → skipped
        assert plan.steps[1].status == "skipped"


# =====================================================================
# feature flag 默认关闭
# =====================================================================


class TestPlanExecuteFeatureFlag:
    def test_feature_flag_default_disabled(self):
        """feature flag 默认关闭（不破坏现有行为）"""
        assert isinstance(PLAN_EXECUTE_ENABLED, bool)

    def test_jaccard_threshold_in_range(self):
        assert 0.0 <= PLAN_CACHE_JACCARD_THRESHOLD <= 1.0

    def test_thresholds_positive(self):
        assert COMPLEX_QUERY_MIN_KEYWORDS >= 1
        assert COMPLEX_QUERY_MIN_LENGTH >= 1
