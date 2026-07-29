"""P4.4 A2A 1.2 升级 - 测试矩阵

覆盖点：
1. test_send_subscribe_sse: SSE 流式
2. test_send_push_webhook: Webhook 推送
3. test_agent_card_push_notifications_flag: pushNotifications 标志
4. test_disabled_returns_v1: feature flag 关闭行为不变
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import deadman.a2a.models as a2a_models
import deadman.a2a.server as a2a_server_module
from deadman.a2a.models import (
    A2A_V12_ENABLED,
    AgentCard,
    AgentCardSkill,
    PushNotificationConfig,
    TaskState,
)
from deadman.a2a.server import (
    A2AServer,
    _HAS_CRYPTOGRAPHY,
    _HAS_HTTPX,
    format_sse_events,
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_v12(monkeypatch):
    """每个测试默认开启 A2A v1.2 feature flag"""
    monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", True)
    monkeypatch.setattr(a2a_server_module, "A2A_V12_ENABLED", True)
    yield


@pytest.fixture
def v12_server() -> A2AServer:
    """构造一个带 skill 的 v1.2 A2AServer"""
    card = AgentCard(
        name="test-agent",
        description="测试 agent",
        version="1.0",
        url="http://localhost:9000/a2a",
        skills=[
            AgentCardSkill(
                id="test-skill",
                name="测试能力",
                description="用于测试",
                tags=["test"],
            ),
        ],
    )
    return A2AServer(card=card)


@pytest.fixture
def v12_server_with_llm(v12_server, monkeypatch):
    """v12 server + mock LLM（让 tasks/send 能成功）"""
    mock_llm = MagicMock()
    mock_llm.api_key = "test-key"

    async def _mock_chat(messages, temperature=0.3, **kwargs):
        return "mock LLM response"

    mock_llm.chat = AsyncMock(side_effect=_mock_chat)
    # patch llm_client 全局单例
    import deadman.llm as llm_module
    monkeypatch.setattr(llm_module, "llm_client", mock_llm)
    return v12_server


# =====================================================================
# 1. SSE 流式
# =====================================================================


class TestSendSubscribeSse:
    @pytest.mark.asyncio
    async def test_send_subscribe_sse(self, v12_server_with_llm):
        """tasks/sendSubscribe 返回 working + completed 两个 SSE 事件"""
        server = v12_server_with_llm
        req = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "tasks/sendSubscribe",
            "params": {
                "skill_id": "test-skill",
                "message": {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            },
        }
        resp = await server.handle_jsonrpc(req)
        assert resp["_streaming"] is True
        result = resp["result"]
        assert "task" in result
        assert "events" in result
        events = result["events"]
        # 至少有 working 事件
        assert any(e["event"] == "working" for e in events)
        # LLM 可用，应有 completed 事件
        assert any(e["event"] == "completed" for e in events)
        # task 状态为 completed
        assert result["task"]["state"] == "completed"

    @pytest.mark.asyncio
    async def test_send_subscribe_format_sse_wire(self, v12_server_with_llm):
        """sendSubscribe 返回的 events 能被 format_sse_events 转成 SSE wire 格式"""
        server = v12_server_with_llm
        req = {
            "jsonrpc": "2.0",
            "id": "req-2",
            "method": "tasks/sendSubscribe",
            "params": {
                "skill_id": "test-skill",
                "message": {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            },
        }
        resp = await server.handle_jsonrpc(req)
        events = resp["result"]["events"]
        sse_text = format_sse_events(events)
        # SSE wire 格式校验
        assert "event: working" in sse_text
        assert "event: completed" in sse_text
        assert "data: " in sse_text
        # 每条事件后有空行
        assert sse_text.endswith("\n")

    @pytest.mark.asyncio
    async def test_send_subscribe_llm_unavailable_emits_failed(
        self, v12_server, monkeypatch
    ):
        """LLM 不可用时 sendSubscribe 仍返回 working + failed 事件"""
        # mock LLM api_key 为空 → graph 降级 → fallback LLM 也失败 → failed
        mock_llm = MagicMock()
        mock_llm.api_key = ""
        import deadman.llm as llm_module
        monkeypatch.setattr(llm_module, "llm_client", mock_llm)
        # graph 在模块级导入了 llm_client，需要同步 patch
        import deadman.orchestration.nodes as nodes_module
        monkeypatch.setattr(nodes_module, "llm_client", mock_llm, raising=False)

        req = {
            "jsonrpc": "2.0",
            "id": "req-3",
            "method": "tasks/sendSubscribe",
            "params": {
                "skill_id": "test-skill",
                "message": {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            },
        }
        resp = await v12_server.handle_jsonrpc(req)
        events = resp["result"]["events"]
        assert any(e["event"] == "working" for e in events)
        assert any(e["event"] == "failed" for e in events)
        assert resp["result"]["task"]["state"] == "failed"


# =====================================================================
# 2. Webhook 推送
# =====================================================================


class TestSendPushWebhook:
    @pytest.mark.asyncio
    async def test_send_push_webhook_success(self, v12_server_with_llm):
        """tasks/sendPush 成功推送（mock httpx 返回 200）"""
        server = v12_server_with_llm
        # 先创建一个任务
        send_req = {
            "jsonrpc": "2.0", "id": "s1", "method": "tasks/send",
            "params": {
                "skill_id": "test-skill",
                "message": {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            },
        }
        send_resp = await server.handle_jsonrpc(send_req)
        task_id = send_resp["result"]["id"]

        # mock httpx POST 返回 200
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("deadman.a2a.server.httpx.AsyncClient", return_value=mock_client):
            push_req = {
                "jsonrpc": "2.0", "id": "p1", "method": "tasks/sendPush",
                "params": {
                    "task_id": task_id,
                    "webhook_url": "http://example.com/hook",
                    "event_type": "task.completed",
                    "token": "bearer-xyz",
                },
            }
            resp = await server.handle_jsonrpc(push_req)

        assert "result" in resp
        assert resp["result"]["pushed"] is True
        assert resp["result"]["status_code"] == 200
        # 校验 httpx 被正确调用（带 Authorization header）
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer bearer-xyz"

    @pytest.mark.asyncio
    async def test_send_push_task_not_found(self, v12_server):
        """task 不存在时返回 -32602 错误"""
        push_req = {
            "jsonrpc": "2.0", "id": "p2", "method": "tasks/sendPush",
            "params": {
                "task_id": "nonexistent",
                "webhook_url": "http://example.com/hook",
            },
        }
        resp = await v12_server.handle_jsonrpc(push_req)
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    @pytest.mark.asyncio
    async def test_send_push_httpx_failure(self, v12_server_with_llm):
        """httpx 调用失败时返回 pushed=False"""
        server = v12_server_with_llm
        # 创建任务
        send_req = {
            "jsonrpc": "2.0", "id": "s1", "method": "tasks/send",
            "params": {
                "skill_id": "test-skill",
                "message": {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            },
        }
        send_resp = await server.handle_jsonrpc(send_req)
        task_id = send_resp["result"]["id"]

        # mock httpx POST 抛异常
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("network down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("deadman.a2a.server.httpx.AsyncClient", return_value=mock_client):
            push_req = {
                "jsonrpc": "2.0", "id": "p3", "method": "tasks/sendPush",
                "params": {
                    "task_id": task_id,
                    "webhook_url": "http://example.com/hook",
                },
            }
            resp = await server.handle_jsonrpc(push_req)

        assert resp["result"]["pushed"] is False
        assert "ConnectionError" in resp["result"]["error"]


# =====================================================================
# 3. AgentCard pushNotifications 标志
# =====================================================================


class TestAgentCardPushNotificationsFlag:
    def test_agent_card_push_notifications_flag_v12_enabled(self):
        """v1.2 开启时 AgentCard.capabilities.pushNotifications 默认为 True"""
        card = AgentCard(name="x", description="", version="1", url="http://x")
        # __post_init__ 已在 v1.2 开启时 setdefault True
        assert card.capabilities.get("pushNotifications") is True

    def test_agent_card_push_notifications_flag_v1_default(self, monkeypatch):
        """v1.2 关闭时 pushNotifications 保持 v1.0 默认 False"""
        monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", False)
        card = AgentCard(name="x", description="", version="1", url="http://x")
        assert card.capabilities.get("pushNotifications") is False

    def test_push_notification_config_serialization(self):
        """PushNotificationConfig to_dict/from_dict 往返"""
        cfg = PushNotificationConfig(
            url="http://example.com/hook",
            token="secret",
            event_types=["task.completed", "task.failed"],
        )
        d = cfg.to_dict()
        assert d["url"] == "http://example.com/hook"
        assert d["token"] == "secret"
        assert d["event_types"] == ["task.completed", "task.failed"]
        # from_dict 往返
        cfg2 = PushNotificationConfig.from_dict(d)
        assert cfg2.url == cfg.url
        assert cfg2.token == cfg.token
        assert cfg2.event_types == cfg.event_types

    def test_agent_card_to_dict_includes_push_config_when_v12(self):
        """v1.2 开启 + card 有 push_notification_config 时 to_dict 包含该字段"""
        cfg = PushNotificationConfig(url="http://hook.example.com")
        card = AgentCard(
            name="x", description="", version="1", url="http://x",
            push_notification_config=cfg,
        )
        d = card.to_dict()
        assert "pushNotificationConfig" in d
        assert d["pushNotificationConfig"]["url"] == "http://hook.example.com"

    def test_agent_card_to_dict_excludes_push_config_when_v1(self, monkeypatch):
        """v1.2 关闭时 to_dict 不包含 pushNotificationConfig（即使设置了）"""
        monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", False)
        cfg = PushNotificationConfig(url="http://hook.example.com")
        card = AgentCard(
            name="x", description="", version="1", url="http://x",
            push_notification_config=cfg,
        )
        d = card.to_dict()
        assert "pushNotificationConfig" not in d


# =====================================================================
# 4. feature flag 关闭行为不变
# =====================================================================


class TestDisabledReturnsV1:
    @pytest.mark.asyncio
    async def test_disabled_returns_v1_for_send_subscribe(self, monkeypatch):
        """v1.2 关闭时 sendSubscribe 返回 -32601（method not found）"""
        monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", False)
        monkeypatch.setattr(a2a_server_module, "A2A_V12_ENABLED", False)
        server = A2AServer()
        req = {
            "jsonrpc": "2.0", "id": "x", "method": "tasks/sendSubscribe",
            "params": {},
        }
        resp = await server.handle_jsonrpc(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32601
        assert "v1.2 disabled" in resp["error"]["message"]

    @pytest.mark.asyncio
    async def test_disabled_returns_v1_for_send_push(self, monkeypatch):
        """v1.2 关闭时 sendPush 返回 -32601"""
        monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", False)
        monkeypatch.setattr(a2a_server_module, "A2A_V12_ENABLED", False)
        server = A2AServer()
        req = {
            "jsonrpc": "2.0", "id": "x", "method": "tasks/sendPush",
            "params": {},
        }
        resp = await server.handle_jsonrpc(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_disabled_v1_methods_still_work(self, monkeypatch):
        """v1.2 关闭时 v1.0 方法（send/get/cancel）仍正常工作"""
        monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", False)
        monkeypatch.setattr(a2a_server_module, "A2A_V12_ENABLED", False)
        server = A2AServer()
        # tasks/get 对不存在的 task 返回 -32602（证明路由正常）
        req = {
            "jsonrpc": "2.0", "id": "x", "method": "tasks/get",
            "params": {"task_id": "nonexistent"},
        }
        resp = await server.handle_jsonrpc(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32602  # 任务不存在，路由正常

    @pytest.mark.asyncio
    async def test_disabled_signature_returns_none_or_false(self, monkeypatch):
        """v1.2 关闭时签名方法返回 None / False"""
        monkeypatch.setattr(a2a_models, "A2A_V12_ENABLED", False)
        monkeypatch.setattr(a2a_server_module, "A2A_V12_ENABLED", False)
        server = A2AServer()
        assert server.sign_agent_card() is None
        assert server.verify_agent_card_signature("dummy", "deadbeef") is False


# =====================================================================
# 5. 签名认证（cryptography 可选）
# =====================================================================


class TestAgentCardSignature:
    def test_sign_and_verify_roundtrip(self, v12_server):
        """签名 → 校验 往返（cryptography 可用时）"""
        if not _HAS_CRYPTOGRAPHY:
            pytest.skip("cryptography 不可用，跳过签名测试")
        server = v12_server
        # 用自动生成的临时密钥对签名
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        signature = server.sign_agent_card(private_key_pem=private_pem)
        assert signature is not None
        # 校验通过
        assert server.verify_agent_card_signature(public_pem, signature) is True
        # 错误的签名校验失败
        assert server.verify_agent_card_signature(public_pem, "deadbeef") is False

    def test_sign_returns_none_when_cryptography_missing(self, v12_server, monkeypatch):
        """cryptography 不可用时签名返回 None"""
        monkeypatch.setattr(a2a_server_module, "_HAS_CRYPTOGRAPHY", False)
        assert v12_server.sign_agent_card() is None
