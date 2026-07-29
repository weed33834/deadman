"""测试 deadman.web.server._handle_chat 走 graph 编排链

覆盖 P0-1 修复（docs/pm-assessment.md）：
- _handle_chat 调 build_main_graph().ainvoke 而非 llm_client.chat
- 传入 state 含 user_input/agent_name/user_id/history
- 返回值含 risk_tier
- 调 MemoryManager.after_turn 更新记忆
- graph 失败时降级到 llm_client 但仍返回响应
- 降级路径用 SoulLoader.default_soul 而非硬编码 prompt
- _handle_whoami 返回 is_ai=True 和 disclaimer
- /api/whoami GET/POST 都能访问
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deadman.types import RiskTier, RuleCheckResult
from deadman.web.server import WebServer

# =====================================================================
# 辅助函数 / fixtures
# =====================================================================


@pytest.fixture
def web_server() -> WebServer:
    """构造一个 WebServer 实例"""
    return WebServer()


def _make_mock_graph(result_state: dict) -> MagicMock:
    """构造 mock graph，ainvoke 返回 result_state"""
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=result_state)
    return mock_graph


def _make_mock_mm_class() -> MagicMock:
    """构造 mock MemoryManager 类，after_turn 为 AsyncMock"""
    mock_class = MagicMock()
    mock_class.return_value.after_turn = AsyncMock()
    return mock_class


def _get_free_port() -> int:
    """获取一个可用端口"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(port: int, timeout: float = 5.0) -> bool:
    """等待服务器就绪（轮询 /api/health）"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            conn.close()
            if resp.status == 200:
                return True
        except (ConnectionError, OSError):
            pass
        time.sleep(0.1)
    return False


# =====================================================================
# _handle_chat 走 graph
# =====================================================================


class TestHandleChatGraph:
    """验证 _handle_chat 走 graph 编排链（P0-1 修复）"""

    async def test_handle_chat_calls_graph(self, web_server: WebServer):
        """_handle_chat 调 graph.ainvoke 而非 llm_client.chat"""
        mock_graph = _make_mock_graph({
            "final_response": "graph-response",
            "current_agent": "death_aftercare",
            "rule_check": None,
        })
        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(return_value="should-not-be-called")

        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ), patch(
            "deadman.llm.llm_client", mock_llm
        ):
            result = await web_server._handle_chat(
                agent="death-aftercare",
                query="test query",
                history=[],
                user_id="test-user",
            )

        assert mock_graph.ainvoke.called, "graph.ainvoke 应被调用"
        assert not mock_llm.chat.called, "llm_client.chat 不应被调用（走 graph 路径）"
        assert result["response"] == "graph-response"
        assert result["degraded"] is False

    async def test_handle_chat_passes_state(self, web_server: WebServer):
        """传入 state 含 user_input/agent_name/user_id/history"""
        captured_state: list[dict] = []

        async def capture_ainvoke(state, config=None):
            captured_state.append(dict(state))
            return {
                "final_response": "response",
                "current_agent": "death_aftercare",
                "rule_check": None,
            }

        mock_graph = MagicMock()
        mock_graph.ainvoke = capture_ainvoke

        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ):
            await web_server._handle_chat(
                agent="death-aftercare",
                query="我的问题",
                history=[{"role": "user", "content": "历史1"}],
                user_id="user-123",
            )

        assert len(captured_state) == 1
        state = captured_state[0]
        assert state["user_input"] == "我的问题"
        # agent_name 字段（前端传入的短横线格式被归一化为下划线）
        assert state["agent_name"] == "death_aftercare"
        # user_id 字段
        assert state["user_id"] == "user-123"
        # history 字段
        assert "history" in state
        assert len(state["history"]) == 1
        assert state["history"][0]["content"] == "历史1"

    async def test_handle_chat_returns_risk_tier(self, web_server: WebServer):
        """返回值含 risk_tier"""
        mock_graph = _make_mock_graph({
            "final_response": "response",
            "current_agent": "death_aftercare",
            "rule_check": RuleCheckResult(
                passed=False,
                violations=[{"rule": "test-rule", "priority": 1}],
                risk_tier=RiskTier.R2,
                safety_triggered=False,
            ),
        })

        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ):
            result = await web_server._handle_chat(
                agent="death-aftercare",
                query="test",
                history=[],
                user_id="u1",
            )

        assert "risk_tier" in result
        assert result["risk_tier"] == "R2"
        assert "rule_violations" in result
        assert len(result["rule_violations"]) == 1
        assert result["safety_triggered"] is False
        assert result["degraded"] is False

    async def test_handle_chat_updates_memory(self, web_server: WebServer):
        """调 MemoryManager.after_turn 更新记忆"""
        mock_graph = _make_mock_graph({
            "final_response": "memory-response",
            "current_agent": "death_aftercare",
            "rule_check": None,
        })
        mock_mm_class = _make_mock_mm_class()

        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", mock_mm_class
        ):
            result = await web_server._handle_chat(
                agent="death-aftercare",
                query="test query",
                history=[],
                user_id="user-456",
            )

        assert mock_mm_class.return_value.after_turn.called, "after_turn 应被调用"
        call_kwargs = mock_mm_class.return_value.after_turn.call_args.kwargs
        assert call_kwargs["user_id"] == "user-456"
        assert call_kwargs["user_input"] == "test query"
        assert call_kwargs["assistant_response"] == "memory-response"
        assert result["degraded"] is False

    async def test_handle_chat_fallback_on_graph_failure(self, web_server: WebServer):
        """graph 失败时降级到 llm_client 但仍返回响应"""
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph boom"))
        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(return_value="fallback-response")

        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.llm.llm_client", mock_llm
        ):
            result = await web_server._handle_chat(
                agent="death-aftercare",
                query="test query",
                history=[],
                user_id="test-user",
            )

        assert result["degraded"] is True
        assert result["response"] == "fallback-response"
        assert "error" in result
        assert "graph boom" in result["error"]
        assert result.get("degraded_reason") == "graph_failed_using_fallback"

    async def test_handle_chat_no_hardcoded_system_prompt(self, web_server: WebServer):
        """降级路径用 SoulLoader.default_soul 而非硬编码 prompt"""
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph boom"))
        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(return_value="fallback-response")

        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.llm.llm_client", mock_llm
        ):
            result = await web_server._handle_chat(
                agent="death-aftercare",
                query="test query",
                history=[],
                user_id="test-user",
            )

        assert result["degraded"] is True
        # 验证 llm_client.chat 被调用
        assert mock_llm.chat.called
        # 捕获传入的 messages
        call_args = mock_llm.chat.call_args
        messages = call_args.args[0]
        assert len(messages) > 0
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        # SoulLoader.default_soul 包含 "deadman" 和 "服务边界"
        assert "deadman" in system_msg["content"]
        # 不应包含旧的硬编码 prompt
        assert "你是 death-aftercare 智能体" not in system_msg["content"]
        assert "专注于协助处理逝者身后事" not in system_msg["content"]


# =====================================================================
# _handle_whoami
# =====================================================================


class TestHandleWhoami:
    """验证 _handle_whoami 平台身份告知（transparency-framework L5）"""

    def test_handle_whoami(self, web_server: WebServer):
        """_handle_whoami 返回 is_ai=True 和 disclaimer"""
        result = web_server._handle_whoami()
        assert result["is_ai"] is True
        assert "disclaimer" in result
        assert "不代办" in result["disclaimer"]
        assert result["platform"] == "deadman"
        assert "agents" in result
        assert len(result["agents"]) == 6
        assert "death-aftercare" in result["agents"]
        assert result["rules_count"] == 15
        assert "supported_languages" in result

    def test_handle_whoami_get_and_post(self):
        """/api/whoami GET 和 POST 都能访问"""
        port = _get_free_port()
        server = WebServer()
        thread = threading.Thread(
            target=server.run,
            args=("127.0.0.1", port),
            daemon=True,
        )
        thread.start()

        try:
            assert _wait_for_server(port), "服务器未在超时内启动"

            # GET
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/whoami")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["is_ai"] is True
            assert "disclaimer" in data
            conn.close()

            # POST
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/whoami", body="{}")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["is_ai"] is True
            assert "disclaimer" in data
            conn.close()
        finally:
            pass  # daemon 线程会随进程退出


# =====================================================================
# P3：_stream_chat 推送 trace 事件（思考过程可视化）
# =====================================================================


class TestStreamChatTracePush:
    """验证 _stream_chat 在 graph 返回 trace_spans 时推送 event: trace

    覆盖点：
    - graph 返回 trace_spans + subagent_called + metrics → SSE 流含 event: trace
    - trace payload 字段完整（spans/subagent_called/metrics/agent/draft_response）
    - 降级路径（graph 异常）不推送 trace
    - 空 trace_spans（graph 跑通但无 span）不推送 trace
    """

    @staticmethod
    def _make_wfile() -> MagicMock:
        """构造 mock wfile，记录所有 write 调用便于断言"""
        wfile = MagicMock()
        # write 接受 bytes，flush 不报错
        wfile.write = MagicMock(side_effect=lambda data: len(data) if isinstance(data, (bytes, bytearray)) else 0)
        wfile.flush = MagicMock()
        return wfile

    @staticmethod
    def _collect_written(wfile: MagicMock) -> str:
        """把所有 write 调用的 bytes 参数拼成一个字符串"""
        chunks = []
        for call in wfile.write.call_args_list:
            args, _ = call
            if args and isinstance(args[0], (bytes, bytearray)):
                chunks.append(args[0].decode("utf-8", errors="replace"))
        return "".join(chunks)

    async def test_stream_pushes_trace_event_when_graph_returns_spans(
        self, web_server: WebServer
    ):
        """graph 返回 trace_spans 时，SSE 流应包含 event: trace"""
        mock_graph = _make_mock_graph({
            "final_response": "完整响应。",
            "draft_response": "草稿。",
            "current_agent": "death_aftercare",
            "rule_check": None,
            "trace_spans": [
                {"span_type": "rule", "name": "node.input_guard",
                 "attributes": {"passed": True}},
                {"span_type": "agent", "name": "node.agent.death_aftercare",
                 "attributes": {"tool_name": "query_knowledge",
                                "tool_status": "ok",
                                "tool_result": {"hits": 3}}},
            ],
            "subagent_called": ["death-aftercare-emotional"],
            "metrics": {"tokens": 128, "latency_ms": 420},
        })

        wfile = self._make_wfile()
        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ):
            await web_server._stream_chat(wfile, "test", "death-aftercare", "u1")

        written = self._collect_written(wfile)
        # 应包含 event: trace 行
        assert "event: trace" in written, f"未推送 trace 事件，实际写入:\n{written}"
        # 应包含 event: done 行
        assert "event: done" in written
        # trace 应在 done 之前
        assert written.index("event: trace") < written.index("event: done")

        # 提取 trace 的 data 并校验字段
        # 形如：event: trace\ndata: {...}\n\n
        import re as _re
        m = _re.search(r"event: trace\ndata: (.+)\n\n", written)
        assert m, "trace 事件 data 行未找到"
        payload = json.loads(m.group(1))
        assert payload["agent"] == "death-aftercare"
        assert payload["degraded"] is False
        assert payload["draft_response"] == "草稿。"
        assert len(payload["spans"]) == 2
        assert payload["spans"][0]["name"] == "node.input_guard"
        assert payload["spans"][1]["attributes"]["tool_name"] == "query_knowledge"
        assert payload["subagent_called"] == ["death-aftercare-emotional"]
        assert payload["metrics"]["tokens"] == 128

    async def test_stream_done_has_has_trace_flag(self, web_server: WebServer):
        """done 事件应携带 has_trace 标记，前端据此知道是否有思考面板"""
        mock_graph = _make_mock_graph({
            "final_response": "回复。",
            "current_agent": "death-aftercare",
            "rule_check": None,
            "trace_spans": [{"span_type": "rule", "name": "x", "attributes": {}}],
        })
        wfile = self._make_wfile()
        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ):
            await web_server._stream_chat(wfile, "q", "death-aftercare", "u1")

        written = self._collect_written(wfile)
        import re as _re
        m = _re.search(r"event: done\ndata: (.+)\n\n", written)
        assert m
        done = json.loads(m.group(1))
        assert done["has_trace"] is True
        assert done["agent"] == "death-aftercare"

    async def test_stream_no_trace_when_spans_empty(self, web_server: WebServer):
        """graph 跑通但 trace_spans 为空 → 不推送 trace 事件"""
        mock_graph = _make_mock_graph({
            "final_response": "回复。",
            "current_agent": "death-aftercare",
            "rule_check": None,
            "trace_spans": [],
            "subagent_called": [],
            "metrics": {},
        })
        wfile = self._make_wfile()
        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ):
            await web_server._stream_chat(wfile, "q", "death-aftercare", "u1")

        written = self._collect_written(wfile)
        assert "event: trace" not in written, "空 trace 不应推送"
        # done 事件中 has_trace 应为 False
        import re as _re
        m = _re.search(r"event: done\ndata: (.+)\n\n", written)
        assert m
        done = json.loads(m.group(1))
        assert done["has_trace"] is False

    async def test_stream_no_trace_on_degraded_path(self, web_server: WebServer):
        """graph 异常降级到 llm_client 时，不应推送 trace"""
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph 挂了"))

        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(return_value="降级响应")

        wfile = self._make_wfile()
        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ), patch(
            "deadman.llm.llm_client", mock_llm
        ):
            await web_server._stream_chat(wfile, "q", "death-aftercare", "u1")

        written = self._collect_written(wfile)
        assert "event: trace" not in written, "降级路径不应推送 trace"
        assert "event: done" in written
        # 降级路径的 done 仍应有 has_trace=False
        import re as _re
        m = _re.search(r"event: done\ndata: (.+)\n\n", written)
        assert m
        done = json.loads(m.group(1))
        assert done["has_trace"] is False
        assert done["degraded"] is True

    async def test_stream_no_trace_when_llm_no_key(self, web_server: WebServer):
        """降级路径下 LLM key 缺失 → 推送 error 事件后直接返回，无 trace 无 done"""
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph 挂了"))

        mock_llm = MagicMock()
        mock_llm.api_key = ""  # 无 key
        mock_llm.chat = AsyncMock(return_value="should-not-call")

        wfile = self._make_wfile()
        with patch(
            "deadman.orchestration.graph.build_main_graph", return_value=mock_graph
        ), patch(
            "deadman.memory.manager.MemoryManager", _make_mock_mm_class()
        ), patch(
            "deadman.llm.llm_client", mock_llm
        ):
            await web_server._stream_chat(wfile, "q", "death-aftercare", "u1")

        written = self._collect_written(wfile)
        assert "event: error" in written
        assert "event: trace" not in written
        assert "event: done" not in written

