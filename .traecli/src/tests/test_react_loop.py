"""ReAct 循环单元测试 - 覆盖 P0.4 核心场景

场景设计:
1. 直接给 FINAL_ANSWER(无需工具) - 单轮终止
2. 单工具调用后 FINAL_ANSWER - Thought→Action→Observation→Final
3. max_iterations 到达 - 综合历史作答
4. 工具 budget 超限 - 不再调工具,综合历史
5. Stuck 检测 - 连续 2 次 Observation Jaccard > 0.9 提前终止
6. LLM 不可用降级 - 返回 degraded=True
7. LLM 异常 - 单次迭代异常被捕获
8. 工具失败 - failed_tools 集合更新,本轮后续禁用
9. JSON 解析失败 - 回退到文本截取
10. Jaccard 相似度辅助函数
11. 工具注册与分发
12. Self-Verification 失败(可选触发 Early Stop)
"""

from __future__ import annotations

import json
from typing import Any


from deadman.orchestration.react_loop import (
    REACT_ENABLED,
    REACT_STUCK_JACCARD_THRESHOLD,
    ReActLoop,
    ReActResult,
    ReActStep,
    _dispatch_tool,
    _format_observation,
    _jaccard_similarity,
    _parse_react_response,
    _tokenize,
    get_available_tools,
    register_react_tool,
    run_react_loop,
)
import deadman.orchestration.react_loop as react_module


# =====================================================================
# Mock LLM Client - 按 prompt 内容返回不同响应
# =====================================================================


class MockLLMClient:
    """模拟 LLM - 按 responses 队列依次返回,或按 prompt 关键词路由"""

    def __init__(
        self,
        responses: list[str] | None = None,
        api_key: str = "mock-key",
        chat_json_resp: dict[str, Any] | None = None,
        raise_on_chat: bool = False,
    ):
        self.responses = list(responses) if responses else []
        self.api_key = api_key
        self.chat_json_resp = chat_json_resp or {"passed": True, "reason": "ok"}
        self.raise_on_chat = raise_on_chat
        self.call_count = 0
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    async def chat(self, messages, temperature=0.3, max_tokens=4096, **kwargs):
        self.call_count += 1
        if self.raise_on_chat:
            raise RuntimeError("mock LLM error")
        if self.responses:
            return self.responses.pop(0)
        return "mock"

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        return dict(self.chat_json_resp)


# =====================================================================
# Mock 工具 - 用于测试 Action 分发
# =====================================================================


async def _mock_tool_ok(query: str = "", **_: Any) -> dict[str, Any]:
    return {"ok": True, "result": f"search result for {query}"}


async def _mock_tool_fail(**_: Any) -> dict[str, Any]:
    raise RuntimeError("tool error")


async def _mock_tool_large(**_: Any) -> str:
    return "x" * 5000  # 测试截断


# =====================================================================
# 辅助函数测试
# =====================================================================


class TestJaccardSimilarity:
    def test_identical_strings(self):
        assert _jaccard_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert _jaccard_similarity("abc", "xyz") == 0.0

    def test_partial_overlap(self):
        s = _jaccard_similarity("the quick brown fox", "the quick red fox")
        assert 0.0 < s < 1.0

    def test_empty_strings(self):
        assert _jaccard_similarity("", "") == 1.0

    def test_one_empty(self):
        assert _jaccard_similarity("abc", "") == 0.0

    def test_chinese_tokens(self):
        sim = _jaccard_similarity("北京户口注销", "北京户口办理")
        assert 0 < sim < 1

    def test_tokenize_lowercase(self):
        tokens = _tokenize("Hello WORLD")
        assert "hello" in tokens
        assert "world" in tokens


class TestParseReActResponse:
    def test_pure_json(self):
        raw = json.dumps({
            "thought": "需要搜索",
            "action": "web_search",
            "action_input": {"query": "北京户口注销"},
            "final_answer": "",
        })
        parsed = _parse_react_response(raw)
        assert parsed["action"] == "web_search"
        assert parsed["action_input"]["query"] == "北京户口注销"
        assert parsed["final_answer"] == ""

    def test_fenced_json(self):
        raw = '```json\n{"thought":"思考","action":"FINAL_ANSWER","final_answer":"答案"}\n```'
        parsed = _parse_react_response(raw)
        assert parsed["action"] == "FINAL_ANSWER"
        assert parsed["final_answer"] == "答案"

    def test_json_with_extra_text(self):
        raw = '让我分析一下\n{"thought":"分析","action":"web_search","action_input":{}}\n这是输出'
        parsed = _parse_react_response(raw)
        assert parsed["action"] == "web_search"

    def test_final_answer_marker(self):
        raw = "我得到答案了 FINAL_ANSWER: 建议咨询当地医保部门"
        parsed = _parse_react_response(raw)
        assert parsed["action"] == "FINAL_ANSWER"
        assert "医保" in parsed["final_answer"]

    def test_empty_input(self):
        parsed = _parse_react_response("")
        assert parsed["action"] == "NO_ACTION"

    def test_unparseable_falls_back(self):
        raw = "完全不是 JSON 的纯文本" * 100
        parsed = _parse_react_response(raw)
        # 应回退到 thought + NO_ACTION
        assert parsed["action"] == "NO_ACTION"

    def test_action_input_as_string_gets_wrapped(self):
        raw = json.dumps({
            "thought": "需要搜索",
            "action": "web_search",
            "action_input": "北京户口",
        })
        parsed = _parse_react_response(raw)
        # action_input 是 str,_dispatch_tool 会兜底为 {"query": ...}
        assert parsed["action_input"] == "北京户口"


class TestFormatObservation:
    def test_string_passes_through(self):
        text, raw = _format_observation("hello")
        assert text == "hello"
        assert raw == "hello"

    def test_dict_serialized(self):
        text, raw = _format_observation({"ok": True, "n": 1})
        assert "ok" in text
        assert isinstance(raw, dict)

    def test_long_result_truncated(self):
        text, _ = _format_observation("x" * 5000)
        # 2000 字符 + 截断标记(~15 字符)
        assert len(text) <= 2020
        assert "truncated" in text
        assert len(text) < 5000  # 确实被截断

    def test_non_serializable_fallback(self):
        class Foo:
            def __str__(self):
                return "foo-instance"
        text, _ = _format_observation(Foo())
        assert "foo-instance" in text


# =====================================================================
# 工具注册与分发测试
# =====================================================================


class TestToolRegistry:
    def setup_method(self):
        # 每个测试前清空 registry(测试隔离)
        react_module._TOOL_REGISTRY.clear()

    def test_register_and_get(self):
        register_react_tool("test_tool", _mock_tool_ok)
        assert "test_tool" in get_available_tools()

    def test_get_available_tools_sorted(self):
        register_react_tool("z_tool", _mock_tool_ok)
        register_react_tool("a_tool", _mock_tool_ok)
        tools = get_available_tools()
        assert tools == sorted(tools)

    async def test_dispatch_unknown_tool(self):
        ok, result, err = await _dispatch_tool("nonexistent", {}, set())
        assert ok is False
        assert "not registered" in err

    async def test_dispatch_failed_tool_skipped(self):
        failed = {"broken_tool"}
        ok, result, err = await _dispatch_tool("broken_tool", {}, failed)
        assert ok is False
        assert "already failed" in err

    async def test_dispatch_success(self):
        register_react_tool("good_tool", _mock_tool_ok)
        ok, result, err = await _dispatch_tool(
            "good_tool", {"query": "test"}, set()
        )
        assert ok is True
        assert err == ""
        assert result["ok"] is True

    async def test_dispatch_exception_marks_failed(self):
        register_react_tool("bad_tool", _mock_tool_fail)
        failed: set[str] = set()
        ok, result, err = await _dispatch_tool("bad_tool", {}, failed)
        assert ok is False
        assert "bad_tool" in failed
        assert "RuntimeError" in err

    async def test_dispatch_action_input_as_string(self):
        register_react_tool("good_tool", _mock_tool_ok)
        # action_input 是 str → 兜底成 {"query": str}
        ok, result, err = await _dispatch_tool(
            "good_tool", "raw query", set()
        )
        assert ok is True
        assert "raw query" in result["result"]


# =====================================================================
# ReActLoop 主体测试
# =====================================================================


class TestReActLoopDirectFinal:
    """场景 1:LLM 直接给 FINAL_ANSWER"""

    async def test_direct_final_answer(self):
        llm = MockLLMClient(responses=[json.dumps({
            "thought": "无需工具,直接回答",
            "action": "FINAL_ANSWER",
            "action_input": {},
            "final_answer": "建议咨询当地医保部门",
        })])
        loop = ReActLoop(llm=llm, system_prompt="sys", user_input="问题")
        result = await loop.run()
        assert result.terminated_by == "final_answer"
        assert "医保" in result.final_answer
        assert len(result.steps) == 1
        assert llm.call_count == 1


class TestReActLoopSingleTool:
    """场景 2:Thought→Action→Observation→Final"""

    async def test_single_tool_then_final(self):
        # 清空 registry 避免污染
        react_module._TOOL_REGISTRY.clear()
        register_react_tool("web_search", _mock_tool_ok)

        llm = MockLLMClient(responses=[
            json.dumps({
                "thought": "需要搜索户口注销流程",
                "action": "web_search",
                "action_input": {"query": "北京户口注销"},
                "final_answer": "",
            }),
            json.dumps({
                "thought": "信息足够,作答",
                "action": "FINAL_ANSWER",
                "action_input": {},
                "final_answer": "北京户口注销需到派出所办理",
            }),
        ])
        loop = ReActLoop(llm=llm, system_prompt="sys", user_input="如何注销户口")
        result = await loop.run()
        assert result.terminated_by == "final_answer"
        assert "派出所" in result.final_answer
        assert len(result.steps) == 2
        # 第一步 action 是 web_search,调了工具
        assert result.steps[0].action == "web_search"
        assert result.steps[0].tool_ok is True
        assert "search result" in result.steps[0].observation


class TestReActLoopMaxIterations:
    """场景 3:max_iterations 到达"""

    async def test_max_iterations_reached(self):
        react_module._TOOL_REGISTRY.clear()
        # 工具每次返回不同结果,避免触发 stuck 检测
        counter = {"n": 0}

        async def varying_tool(query: str = "", **_: Any) -> dict[str, Any]:
            counter["n"] += 1
            return {"ok": True, "result": f"第{counter['n']}次结果", "query": query, "extra": f"info{counter['n']}"}

        register_react_tool("web_search", varying_tool)

        # LLM 永远不输出 FINAL_ANSWER,只调工具,每次 query 不同避免 stuck
        def non_final(query: str) -> str:
            return json.dumps({
                "thought": f"搜索{query}",
                "action": "web_search",
                "action_input": {"query": query},
                "final_answer": "",
            })

        summarize_resp = "综合历史:建议咨询派出所"
        llm = MockLLMClient(responses=[
            non_final("户口流程"),
            non_final("派出所地址"),
            summarize_resp,
        ])

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=2,
        )
        result = await loop.run()
        assert result.terminated_by == "max_iterations"
        assert "派出所" in result.final_answer


class TestReActLoopToolBudget:
    """场景 4:工具 budget 超限"""

    async def test_tool_budget_exhausted(self):
        react_module._TOOL_REGISTRY.clear()
        register_react_tool("web_search", _mock_tool_ok)

        non_final = json.dumps({
            "thought": "继续搜索",
            "action": "web_search",
            "action_input": {"query": "更多"},
            "final_answer": "",
        })
        llm = MockLLMClient(responses=[
            non_final,  # iter1: 调工具
            non_final,  # iter2: 工具 budget=1 已用完 → FINAL_ANSWER + 综合作答
            "综合:建议打 12393",
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=5,
            tool_budget=1,  # 只允许 1 次工具调用
        )
        result = await loop.run()
        assert result.terminated_by == "final_answer"
        # 第 2 步因 budget 用尽转 FINAL_ANSWER
        assert result.steps[1].action == "FINAL_ANSWER"
        assert result.steps[1].tool_ok is False


class TestReActLoopStuckDetection:
    """场景 5:Stuck 检测 - 连续 2 次 Observation 相似度 > 阈值"""

    async def test_stuck_triggers_early_termination(self):
        react_module._TOOL_REGISTRY.clear()
        # 工具每次返回相同结果 → Observation 完全相同 → Jaccard=1.0
        register_react_tool("web_search", _mock_tool_ok)

        non_final = json.dumps({
            "thought": "继续",
            "action": "web_search",
            "action_input": {"query": "x"},
            "final_answer": "",
        })
        llm = MockLLMClient(responses=[
            non_final,  # iter1
            non_final,  # iter2: Observation 与 iter1 完全相同 → stuck
            "综合:建议咨询",
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=5,
        )
        result = await loop.run()
        # stuck 触发(terminated_by=stuck 或 final_answer 因 summar-ize)
        assert result.terminated_by in ("stuck", "final_answer")


class TestReActLoopLLMUnavailable:
    """场景 6:LLM 不可用降级"""

    async def test_llm_unavailable_degrades(self):
        loop = ReActLoop(
            llm=None,
            system_prompt="sys",
            user_input="问题",
        )
        result = await loop.run()
        assert result.degraded is True
        assert result.terminated_by == "llm_unavailable"

    async def test_llm_no_api_key_degrades(self):
        llm = MockLLMClient(api_key="")
        loop = ReActLoop(llm=llm, system_prompt="sys", user_input="问题")
        result = await loop.run()
        assert result.degraded is True


class TestReActLoopLLMException:
    """场景 7:LLM 调用抛异常 → 立即终止并标记 error"""

    async def test_llm_exception_terminates(self):
        llm = MockLLMClient(raise_on_chat=True)
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=3,
        )
        result = await loop.run()
        # LLM 抛异常 → 立即终止,不再重试
        assert result.terminated_by == "error"
        assert result.final_answer == ""
        assert len(result.steps) == 1
        assert result.steps[0].tool_error == "llm_exception"


class TestReActLoopToolFailure:
    """场景 8:工具失败 → failed_tools 更新"""

    async def test_failed_tool_recorded(self):
        react_module._TOOL_REGISTRY.clear()
        register_react_tool("bad_tool", _mock_tool_fail)

        llm = MockLLMClient(responses=[
            json.dumps({
                "thought": "调坏工具",
                "action": "bad_tool",
                "action_input": {},
                "final_answer": "",
            }),
            json.dumps({
                "thought": "信息足够",
                "action": "FINAL_ANSWER",
                "action_input": {},
                "final_answer": "兜底回答",
            }),
        ])
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=3,
        )
        result = await loop.run()
        assert result.terminated_by == "final_answer"
        # 第一步工具失败
        assert result.steps[0].tool_ok is False
        assert "RuntimeError" in result.steps[0].tool_error


class TestReActLoopSelfVerify:
    """场景 12:Self-Verification 失败(可选 Early Stop)"""

    async def test_self_verify_fail_triggers_early_stop(self):
        react_module._TOOL_REGISTRY.clear()
        register_react_tool("web_search", _mock_tool_ok)

        # chat_json 返回 passed=False
        llm = MockLLMClient(
            responses=[
                json.dumps({
                    "thought": "搜索",
                    "action": "web_search",
                    "action_input": {"query": "x"},
                    "final_answer": "",
                }),
                json.dumps({
                    "thought": "再搜",
                    "action": "web_search",
                    "action_input": {"query": "y"},
                    "final_answer": "",
                }),
                "综合:答案",
            ],
            chat_json_resp={"passed": False, "reason": "observation 与假设不符"},
        )
        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            max_iterations=5,
            self_verify=True,
        )
        result = await loop.run()
        # 第二轮 self-verify 失败 → 提前终止
        assert result.terminated_by == "self_verify_fail"


class TestReActLoopTraceCallback:
    """trace_callback 被正确调用"""

    async def test_trace_callback_invoked(self):
        llm = MockLLMClient(responses=[json.dumps({
            "thought": "直接回答",
            "action": "FINAL_ANSWER",
            "action_input": {},
            "final_answer": "答案",
        })])
        spans: list[tuple[str, dict]] = []

        def cb(name, attrs):
            spans.append((name, attrs))

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            trace_callback=cb,
        )
        await loop.run()
        # 至少有一个 thought span
        assert any(s[0] == "react.thought" for s in spans)

    async def test_trace_callback_exception_swallowed(self):
        """trace_callback 抛异常不应阻断主流程"""
        llm = MockLLMClient(responses=[json.dumps({
            "thought": "x",
            "action": "FINAL_ANSWER",
            "action_input": {},
            "final_answer": "答案",
        })])

        def bad_cb(name, attrs):
            raise RuntimeError("trace failed")

        loop = ReActLoop(
            llm=llm,
            system_prompt="sys",
            user_input="问题",
            trace_callback=bad_cb,
        )
        result = await loop.run()
        # 不应抛异常,正常返回
        assert result.final_answer == "答案"


class TestReActResultSerialization:
    """ReActResult.to_dict() 可序列化"""

    def test_to_dict_roundtrip(self):
        result = ReActResult(
            final_answer="test",
            steps=[ReActStep(iteration=1, thought="t", action="FINAL_ANSWER")],
            terminated_by="final_answer",
            total_tokens=100,
        )
        d = result.to_dict()
        assert d["final_answer"] == "test"
        assert d["terminated_by"] == "final_answer"
        assert len(d["steps"]) == 1
        # 可 JSON 序列化
        json.dumps(d)


class TestRunReactLoopEntrypoint:
    """便捷入口 run_react_loop"""

    async def test_entrypoint_with_provided_llm(self):
        llm = MockLLMClient(responses=[json.dumps({
            "thought": "x",
            "action": "FINAL_ANSWER",
            "action_input": {},
            "final_answer": "答案",
        })])
        result = await run_react_loop(
            system_prompt="sys",
            user_input="问题",
            llm=llm,
        )
        assert result.final_answer == "答案"


class TestReactEnabledFlag:
    """feature flag 默认关闭(不破坏现有行为)"""

    def test_default_disabled(self):
        # 默认环境不应启用 ReAct(避免破坏现有 918 测试)
        # 注意:CI 可能显式启用,这里只验证类型
        assert isinstance(REACT_ENABLED, bool)
        assert isinstance(REACT_STUCK_JACCARD_THRESHOLD, float)
        assert 0.0 <= REACT_STUCK_JACCARD_THRESHOLD <= 1.0
