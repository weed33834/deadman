"""OpenAI 兼容端点测试（/v1/chat/completions）

覆盖：
- 非流式：200 + chat.completion 结构 + deadman 扩展字段
- 流式：SSE chunk 结构 + [DONE] 终止帧
- 空 messages → 400
- model 字段路由到指定智能体
- 非法 JSON → 400
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from deadman.web.app import app

    return TestClient(app)


class TestNonStream:
    def test_basic_completion(self, client, mock_llm_client, monkeypatch):
        # mock graph 走降级路径即可：handle_chat 内部 graph 失败→LLM 兜底→mock 响应
        import deadman.llm as llm_module

        monkeypatch.setattr(llm_module, "llm_client", mock_llm_client)

        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "deadman/death-aftercare",
                "messages": [{"role": "user", "content": "你好，想咨询身后事流程"}],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert isinstance(body["choices"][0]["message"]["content"], str)
        # OpenAI 客户端会忽略的扩展字段
        assert "deadman" in body

    def test_empty_messages_rejected(self, client):
        r = client.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 400
        assert "user" in r.json()["error"]["message"]

    def test_invalid_json_rejected(self, client):
        r = client.post(
            "/v1/chat/completions",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400


class TestAgentRouting:
    def test_model_field_routes_agent(self, client, monkeypatch):
        """model=legal-advisor 应路由到 legal-advisor 智能体"""
        captured: dict = {}

        async def fake_handle_chat(agent, query, history, user_id=None):
            captured["agent"] = agent
            return {"response": "ok", "degraded": True, "risk_tier": None}

        from deadman.web.routes import openai_compat

        monkeypatch.setattr(openai_compat, "handle_chat", fake_handle_chat)

        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "legal-advisor",
                "messages": [{"role": "user", "content": "遗产纠纷"}],
            },
        )
        assert r.status_code == 200
        assert captured["agent"] == "legal-advisor"
        # 未知 model 名回退默认智能体
        r2 = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r2.status_code == 200


class TestStream:
    def test_stream_chunks_and_done(self, client, monkeypatch):
        async def fake_stream(agent, query, user_id=None):
            yield 'data: {"chunk": "你好"}\n\n'
            yield 'data: {"chunk": "，请节哀"}\n\n'
            yield 'data: {"done": {"has_trace": false}}\n\n'

        from deadman.web.routes import openai_compat

        monkeypatch.setattr(openai_compat, "stream_chat_events", fake_stream)

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"stream": True, "messages": [{"role": "user", "content": "测试"}]},
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
            raw = "".join(r.iter_text())

        frames = [ln[6:] for ln in raw.split("\n") if ln.startswith("data: ")]
        assert frames[-1] == "[DONE]"
        chunks = [json.loads(f) for f in frames[:-1]]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        contents = [
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c["choices"][0]["delta"].get("content")
        ]
        assert "你好" in "".join(contents)

    def test_stream_error_event_becomes_terminal_content(self, client, monkeypatch):
        """graph 失败的 error 事件应转为内容帧并终止，而非静默挂起"""

        async def failing_stream(agent, query, user_id=None):
            yield 'data: {"error": "graph boom"}\n\n'

        from deadman.web.routes import openai_compat

        monkeypatch.setattr(openai_compat, "stream_chat_events", failing_stream)

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"stream": True, "messages": [{"role": "user", "content": "x"}]},
        ) as r:
            raw = "".join(r.iter_text())

        frames = [ln[6:] for ln in raw.split("\n") if ln.startswith("data: ")]
        assert frames[-1] == "[DONE]"
        chunks = [json.loads(f) for f in frames[:-1]]
        content_frames = [c for c in chunks if c["choices"][0]["delta"].get("content")]
        assert (
            content_frames and "graph boom" in content_frames[-1]["choices"][0]["delta"]["content"]
        )
        # 终止帧：最后一个 chunk（内容帧）应带 finish_reason=stop
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
