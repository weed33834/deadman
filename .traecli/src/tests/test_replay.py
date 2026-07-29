"""测试 P6.5 Replay Debugging - ReplayDebugger

覆盖点：
  - feature flag 关闭时返回空 ReplayResult
  - trace 不存在时返回空结果
  - 无 ROOT span 时返回空结果
  - 从 ROOT span 提取 user_input / system_prompt / original_response
  - original_response 兜底从 LLM_JUDGE span 提取
  - LLM 调用失败时 replayed_response 为空、error 非空、improved=False
  - 成功重放时生成 diff
  - _is_improved 判定逻辑（响应变长视为改进）
  - _generate_diff 在内容相同时返回空字符串
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from deadman.observability.replay import (
    ReplayDebugger,
    ReplayRequest,
    ReplayResult,
)
from deadman.observability.tracer import SpanType, Tracer

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def enabled_replay(monkeypatch):
    """临时启用 REPLAY_ENABLED"""
    monkeypatch.setattr(
        "deadman.observability.replay.REPLAY_ENABLED", True
    )
    yield


@pytest.fixture
def mock_llm():
    """构造 mock LLMClient，chat 默认返回较长响应（比 original 长 → improved=True）"""
    client = MagicMock()
    client.chat = AsyncMock(
        return_value="这是一段重放后的响应，比原始响应要长很多，因此会被判定为改进。"
    )
    return client


def _build_trace_with_root(
    user_input: str = "用户原始输入",
    system_prompt: str = "你是助手",
    original_response: str = "原始响应",
) -> tuple[Tracer, str]:
    """构造带 ROOT span 的 Tracer，返回 (tracer, trace_id)"""
    t = Tracer()
    root_id = t.start_span(
        SpanType.ROOT,
        "user_request",
        {
            "user_input": user_input,
            "system_prompt": system_prompt,
            "output": original_response,
        },
    )
    t.end_span(root_id, status="OK")
    spans = t.get_spans()
    return t, spans[0]["trace_id"]


# =====================================================================
# feature flag 关闭
# =====================================================================


class TestReplayDisabled:
    """feature flag 关闭行为测试"""

    @pytest.mark.asyncio
    async def test_replay_disabled_returns_empty(self, mock_llm):
        """feature flag 关闭时 replay 返回空 ReplayResult"""
        from deadman.observability import replay as replay_module

        original = replay_module.REPLAY_ENABLED
        replay_module.REPLAY_ENABLED = False
        try:
            tracer, trace_id = _build_trace_with_root()
            debugger = ReplayDebugger(tracer, mock_llm)
            result = await debugger.replay(ReplayRequest(trace_id=trace_id))

            # 应返回空结果
            assert isinstance(result, ReplayResult)
            assert result.original_response == ""
            assert result.replayed_response == ""
            assert result.diff == ""
            assert result.improved is False
            assert result.metadata.get("reason") == "replay_disabled"
            # LLM 不应被调用
            mock_llm.chat.assert_not_called()
        finally:
            replay_module.REPLAY_ENABLED = original


# =====================================================================
# trace 加载失败 / 无 ROOT span
# =====================================================================


class TestReplayTraceLoading:
    """trace 加载与 span 提取测试"""

    @pytest.mark.asyncio
    async def test_replay_trace_not_found(
        self, enabled_replay, mock_llm
    ):
        """trace_id 不存在时返回空结果"""
        t = Tracer()  # 空 tracer
        debugger = ReplayDebugger(t, mock_llm)
        result = await debugger.replay(
            ReplayRequest(trace_id="non-existent-trace-id")
        )

        assert result.replayed_response == ""
        assert result.improved is False
        assert result.metadata.get("reason") == "trace_not_found"
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_no_root_span(
        self, enabled_replay, mock_llm
    ):
        """trace 中无 ROOT span 时返回空结果（无 user_input）"""
        t = Tracer()
        # 只创建一个 TOOL span（非 ROOT）
        tool_id = t.start_span(
            SpanType.TOOL, "tool.web_search", {"query": "test"}
        )
        t.end_span(tool_id, status="OK")
        spans = t.get_spans()
        trace_id = spans[0]["trace_id"]

        debugger = ReplayDebugger(t, mock_llm)
        result = await debugger.replay(ReplayRequest(trace_id=trace_id))

        # 无 user_input → reason=no_user_input_in_trace
        assert result.replayed_response == ""
        assert result.metadata.get("reason") == "no_user_input_in_trace"
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_tracer_exception(
        self, enabled_replay, mock_llm
    ):
        """tracer.get_trace 抛异常时应返回 trace_load_error，不抛出"""
        bad_tracer = MagicMock()
        bad_tracer.get_trace = MagicMock(
            side_effect=RuntimeError("db connection lost")
        )

        debugger = ReplayDebugger(bad_tracer, mock_llm)
        result = await debugger.replay(ReplayRequest(trace_id="any"))

        assert result.replayed_response == ""
        assert "trace_load_error" in result.metadata.get("reason", "")
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_none_tracer(self, enabled_replay, mock_llm):
        """tracer 为 None 时应返回 trace_not_found，不抛出"""
        debugger = ReplayDebugger(None, mock_llm)
        result = await debugger.replay(ReplayRequest(trace_id="any"))
        assert result.metadata.get("reason") == "trace_not_found"


# =====================================================================
# 从 span 提取上下文
# =====================================================================


class TestExtractOriginalContext:
    """_extract_original_context 行为测试"""

    def test_extract_from_root_span(self):
        """从 ROOT span attributes 提取 user_input / system_prompt / output"""
        spans = [
            {
                "span_type": "ROOT",
                "attributes": {
                    "user_input": "你好",
                    "system_prompt": "你是助手",
                    "output": "你好，有什么可以帮你？",
                },
            }
        ]
        user_input, system_prompt, response = (
            ReplayDebugger._extract_original_context(spans)
        )
        assert user_input == "你好"
        assert system_prompt == "你是助手"
        assert response == "你好，有什么可以帮你？"

    def test_extract_alternative_keys(self):
        """支持 input / query / message 与 response / result 等替代键"""
        spans = [
            {
                "span_type": "ROOT",
                "attributes": {
                    "query": "test query",
                    "system": "sys",
                    "result": "test result",
                },
            }
        ]
        user_input, system_prompt, response = (
            ReplayDebugger._extract_original_context(spans)
        )
        assert user_input == "test query"
        assert system_prompt == "sys"
        assert response == "test result"

    def test_extract_fallback_to_llm_judge_span(self):
        """ROOT span 无 output 时，应从 LLM_JUDGE span 提取 response"""
        spans = [
            {
                "span_type": "ROOT",
                "attributes": {
                    "user_input": "你好",
                    "system_prompt": "你是助手",
                    # 无 output
                },
            },
            {
                "span_type": "LLM_JUDGE",
                "attributes": {
                    "input": "这是 LLM_JUDGE 的 input（即原始响应）"
                },
            },
        ]
        user_input, system_prompt, response = (
            ReplayDebugger._extract_original_context(spans)
        )
        assert user_input == "你好"
        assert system_prompt == "你是助手"
        assert response == "这是 LLM_JUDGE 的 input（即原始响应）"

    def test_extract_empty_spans(self):
        """空 span 列表应返回三个空字符串"""
        user_input, system_prompt, response = (
            ReplayDebugger._extract_original_context([])
        )
        assert user_input == ""
        assert system_prompt == ""
        assert response == ""


# =====================================================================
# 重放主流程
# =====================================================================


class TestReplayFlow:
    """replay 主流程测试"""

    @pytest.mark.asyncio
    async def test_replay_success_generates_diff(
        self, enabled_replay, mock_llm
    ):
        """成功重放应生成 diff，记录原始/重放响应"""
        tracer, trace_id = _build_trace_with_root(
            user_input="你好",
            system_prompt="你是助手",
            original_response="原始响应",
        )

        debugger = ReplayDebugger(tracer, mock_llm)
        result = await debugger.replay(ReplayRequest(trace_id=trace_id))

        assert result.original_response == "原始响应"
        assert result.replayed_response  # 非空
        assert result.diff  # 有 diff
        assert result.error == ""
        # mock_llm 返回较长文本 → improved=True
        assert result.improved is True
        # metadata 应记录 trace_id
        assert result.metadata.get("trace_id") == trace_id
        # LLM 被调用一次
        mock_llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_replay_llm_failure(
        self, enabled_replay
    ):
        """LLM 调用失败时 replayed_response 为空、error 非空、improved=False"""
        bad_llm = MagicMock()
        bad_llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))

        tracer, trace_id = _build_trace_with_root(
            original_response="原始响应"
        )
        debugger = ReplayDebugger(tracer, bad_llm)
        result = await debugger.replay(ReplayRequest(trace_id=trace_id))

        assert result.original_response == "原始响应"
        assert result.replayed_response == ""
        assert "llm_call_failed" in result.error
        assert result.improved is False
        # diff 仍应生成（original vs empty）
        # 不强制断言 diff 内容，仅断言 improved=False

    @pytest.mark.asyncio
    async def test_replay_with_new_prompt(
        self, enabled_replay, mock_llm
    ):
        """new_prompt 应作为 user 消息内容传给 LLM"""
        tracer, trace_id = _build_trace_with_root(
            user_input="原始输入", original_response="原始响应"
        )
        debugger = ReplayDebugger(tracer, mock_llm)
        await debugger.replay(
            ReplayRequest(
                trace_id=trace_id, new_prompt="这是新的 prompt"
            )
        )

        # 检查最后调用参数中 user 消息内容为新 prompt
        call_args = mock_llm.chat.call_args
        messages = call_args.args[0]
        user_msg = next(
            (m for m in messages if m["role"] == "user"), None
        )
        assert user_msg is not None
        assert user_msg["content"] == "这是新的 prompt"

    @pytest.mark.asyncio
    async def test_replay_with_new_temperature(
        self, enabled_replay, mock_llm
    ):
        """new_temperature 应传给 LLM chat 的 temperature 参数"""
        tracer, trace_id = _build_trace_with_root(
            original_response="原始响应"
        )
        debugger = ReplayDebugger(tracer, mock_llm)
        await debugger.replay(
            ReplayRequest(trace_id=trace_id, new_temperature=0.9)
        )

        call_kwargs = mock_llm.chat.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.9

    @pytest.mark.asyncio
    async def test_replay_with_new_model_kwargs(
        self, enabled_replay, mock_llm
    ):
        """new_model 应通过 kwargs 传给 LLM chat"""
        tracer, trace_id = _build_trace_with_root(
            original_response="原始响应"
        )
        debugger = ReplayDebugger(tracer, mock_llm)
        await debugger.replay(
            ReplayRequest(trace_id=trace_id, new_model="gpt-4o")
        )

        call_kwargs = mock_llm.chat.call_args.kwargs
        assert call_kwargs.get("model") == "gpt-4o"

    @pytest.mark.asyncio
    async def test_replay_metadata_records_request_params(
        self, enabled_replay, mock_llm
    ):
        """metadata 应记录请求参数（new_prompt/new_model/new_temperature）"""
        tracer, trace_id = _build_trace_with_root(
            original_response="原始响应"
        )
        debugger = ReplayDebugger(tracer, mock_llm)
        result = await debugger.replay(
            ReplayRequest(
                trace_id=trace_id,
                new_prompt="新 prompt",
                new_model="gpt-4o",
                new_temperature=0.5,
            )
        )

        assert result.metadata.get("new_prompt") == "新 prompt"
        assert result.metadata.get("new_model") == "gpt-4o"
        assert result.metadata.get("new_temperature") == 0.5
        # original_user_input 应被截断到 200 字符以内
        assert "original_user_input" in result.metadata


# =====================================================================
# _generate_diff / _is_improved
# =====================================================================


class TestGenerateDiff:
    """_generate_diff 行为测试"""

    def test_diff_identical_returns_empty(self):
        """两个相同响应应返回空 diff"""
        text = "相同的响应\n第二行"
        assert ReplayDebugger._generate_diff(text, text) == ""

    def test_diff_both_empty_returns_empty(self):
        """两个空响应应返回空 diff"""
        assert ReplayDebugger._generate_diff("", "") == ""

    def test_diff_different_generates_unified_diff(self):
        """不同响应应生成 unified diff，含 fromfile/tofile 头"""
        original = "第一行\n第二行"
        replayed = "第一行\n修改后的第二行"
        diff = ReplayDebugger._generate_diff(original, replayed)

        assert "--- original" in diff
        assert "+++ replayed" in diff
        assert "修改后的第二行" in diff


class TestIsImproved:
    """_is_improved 行为测试"""

    def test_improved_with_error(self):
        """有 error 时返回 False"""
        assert (
            ReplayDebugger._is_improved("短", "很长很长很长很长", "error")
            is False
        )

    def test_improved_empty_replayed(self):
        """replayed 为空时返回 False"""
        assert ReplayDebugger._is_improved("原始", "", "") is False

    def test_improved_original_empty_replayed_nonempty(self):
        """original 为空、replayed 非空时返回 True"""
        assert ReplayDebugger._is_improved("", "有响应", "") is True

    def test_improved_longer_replayed(self):
        """replayed 比 original 长 20% 以上时返回 True"""
        original = "a" * 100
        replayed = "a" * 130  # 1.3 倍
        assert ReplayDebugger._is_improved(original, replayed, "") is True

    def test_improved_shorter_replayed(self):
        """replayed 比 original 短时返回 False"""
        original = "a" * 100
        replayed = "a" * 50
        assert ReplayDebugger._is_improved(original, replayed, "") is False

    def test_improved_same_length(self):
        """replayed 与 original 等长时返回 False（未达 1.2 倍）"""
        original = "a" * 100
        replayed = "b" * 100
        assert ReplayDebugger._is_improved(original, replayed, "") is False


# =====================================================================
# ReplayRequest / ReplayResult 数据模型
# =====================================================================


class TestDataModels:
    """数据模型默认值测试"""

    def test_replay_request_defaults(self):
        req = ReplayRequest()
        assert req.trace_id == ""
        assert req.new_prompt is None
        assert req.new_model is None
        assert req.new_temperature is None

    def test_replay_result_defaults(self):
        r = ReplayResult()
        assert r.original_response == ""
        assert r.replayed_response == ""
        assert r.diff == ""
        assert r.improved is False
        assert r.error == ""
        assert r.metadata == {}

    def test_replay_result_metadata_isolated(self):
        """不同实例的 metadata 应相互独立（default_factory）"""
        r1 = ReplayResult()
        r2 = ReplayResult()
        r1.metadata["k"] = "v"
        assert "k" not in r2.metadata
