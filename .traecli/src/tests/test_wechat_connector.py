"""测试 deadman.gateway.connectors.wechat - 微信公众号连接器

覆盖 13 个测试场景：
    1. test_verify_signature_correct: 正确签名校验通过
    2. test_verify_signature_wrong: 错误签名校验失败
    3. test_verify_signature_no_verify_token: 未配置 verify_token 时跳过校验
    4. test_handle_webhook_parses_text_message: 解析文本消息并注入队列
    5. test_handle_webhook_ignores_non_text: 非文本消息被忽略
    6. test_pairing_start_success: /start <token> 配对成功
    7. test_pairing_start_invalid_token: /start 错误 token 提示
    8. test_unsubscribe_command: 退订命令调 guard.record_unsubscribe
    9. test_help_command: /help 返回帮助文本
    10. test_no_app_id_graceful_degradation: 无 app_id 时 start() 不抛异常
    11. test_no_app_id_handle_webhook_returns_success: 无 app_id 时返回 success
    12. test_send_calls_correct_url: send 调用正确的 URL 与 body
    13. test_poll_yields_injected_messages: poll 拉取已注入消息

测试隔离：所有 httpx 调用通过 monkeypatch mock，不触达真实网络。
不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.gateway.connectors.wechat import WeChatConnector


# =====================================================================
# 辅助：构造 mock httpx 模块，捕获 AsyncClient 调用
# =====================================================================


class _MockResponse:
    """模拟 httpx.Response"""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or ""

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _MockAsyncClient:
    """模拟 httpx.AsyncClient 上下文管理器"""

    def __init__(self, get_response: _MockResponse | None = None, post_response: _MockResponse | None = None):
        self._get_response = get_response or _MockResponse(json_data={"access_token": "test-token", "expires_in": 7200})
        self._post_response = post_response or _MockResponse(json_data={"errcode": 0, "errmsg": "ok"})
        self.calls: list[dict[str, Any]] = []  # 记录所有调用

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, params: dict[str, Any] | None = None, **kwargs):
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self._get_response

    async def post(self, url: str, params: dict[str, Any] | None = None, json: Any = None, **kwargs):
        self.calls.append({"method": "POST", "url": url, "params": params, "json": json})
        return self._post_response


def _patch_httpx(monkeypatch, client: _MockAsyncClient):
    """把 httpx.AsyncClient 替换为返回 _MockAsyncClient 的工厂"""
    import sys
    import types

    fake_module = types.ModuleType("httpx")
    fake_module.AsyncClient = lambda **kwargs: client  # noqa: E731
    # httpx 已被导入到 wechat 模块内（在函数内部 import）
    # 直接 monkeypatch sys.modules['httpx']，函数内部 import 时会拿到 fake
    monkeypatch.setitem(sys.modules, "httpx", fake_module)


def _make_xml_payload(
    msg_type: str = "text",
    content: str = "你好",
    from_openid: str = "oABC123test",
) -> bytes:
    """构造微信 webhook XML payload"""
    xml = f"""<xml>
<MsgType><![CDATA[{msg_type}]]></MsgType>
<Content><![CDATA[{content}]]></Content>
<FromUserName><![CDATA[{from_openid}]]></FromUserName>
<ToUserName><![CDATA[gh_test123]]></ToUserName>
<CreateTime>1348831860</CreateTime>
<MsgId>1234567890123456</MsgId>
</xml>"""
    return xml.encode("utf-8")


def _make_signature(token: str, timestamp: str, nonce: str) -> str:
    """生成正确的微信签名"""
    parts = sorted([token, timestamp, nonce])
    raw = "".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


# =====================================================================
# 1-3. webhook 签名校验
# =====================================================================


class TestVerifySignature:
    """测试 WeChatConnector._verify_signature"""

    def test_verify_signature_correct(self):
        # 正确签名应通过
        conn = WeChatConnector(
            app_id="wx_app", app_secret="secret", verify_token="mytoken"
        )
        ts = "1348831860"
        nonce = "testnonce"
        sig = _make_signature("mytoken", ts, nonce)
        assert conn._verify_signature(sig, ts, nonce) is True

    def test_verify_signature_wrong(self):
        # 错误签名应失败
        conn = WeChatConnector(
            app_id="wx_app", app_secret="secret", verify_token="mytoken"
        )
        ts = "1348831860"
        nonce = "testnonce"
        wrong_sig = "0" * 40  # 错误签名
        assert conn._verify_signature(wrong_sig, ts, nonce) is False

    def test_verify_signature_no_verify_token(self):
        # 未配置 verify_token 时跳过校验（开发模式）
        conn = WeChatConnector(
            app_id="wx_app", app_secret="secret", verify_token=""
        )
        # 即使签名错误，也应通过（开发模式）
        assert conn._verify_signature("anything", "1", "2") is True


# =====================================================================
# 4-5. webhook 解析消息
# =====================================================================


class TestHandleWebhookParse:
    """测试 handle_webhook 解析消息"""

    def test_handle_webhook_parses_text_message(self, monkeypatch):
        # 文本消息应被解析并注入队列
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
            pairing_tokens={"abc123": "user-1"},
        )
        conn._running = True

        # 先配对 openid → user-1
        conn._paired["oABC123test"] = "user-1"
        conn._user_to_openid["user-1"] = "oABC123test"

        body = _make_xml_payload(content="你好世界")
        ts = "1348831860"
        nonce = "testnonce"
        sig = _make_signature("mytoken", ts, nonce)

        resp = asyncio.run(conn.handle_webhook(body, sig, ts, nonce))
        assert resp == b"success"

        # 队列应有 1 条消息
        assert not conn._queue.empty()
        user_id, text = conn._queue.get_nowait()
        assert user_id == "user-1"
        assert text == "你好世界"

    def test_handle_webhook_ignores_non_text(self, monkeypatch):
        # 非文本消息（如图片）应被忽略
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
        )
        conn._running = True
        conn._paired["oABC123test"] = "user-1"

        body = _make_xml_payload(msg_type="image", content="")
        ts = "1348831860"
        nonce = "testnonce"
        sig = _make_signature("mytoken", ts, nonce)

        resp = asyncio.run(conn.handle_webhook(body, sig, ts, nonce))
        assert resp == b"success"
        # 队列应为空（图片消息被忽略）
        assert conn._queue.empty()


# =====================================================================
# 6-7. 配对 /start <token>
# =====================================================================


class TestPairing:
    """测试 /start 配对命令"""

    def test_pairing_start_success(self, monkeypatch):
        # /start <有效 token> 应配对成功
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
            pairing_tokens={"abc123": "user-1"},
        )
        conn._running = True

        body = _make_xml_payload(content="/start abc123")
        ts = "1"
        nonce = "n"
        sig = _make_signature("mytoken", ts, nonce)

        asyncio.run(conn.handle_webhook(body, sig, ts, nonce))

        # 配对表应记录 openid → user-1
        assert "oABC123test" in conn._paired
        assert conn._paired["oABC123test"] == "user-1"
        # 反查表也应记录
        assert conn._user_to_openid["user-1"] == "oABC123test"
        # 应调用 _send_custom_text 回 POST /message/custom/send
        post_calls = [c for c in mock_client.calls if c["method"] == "POST"]
        assert any("/message/custom/send" in c["url"] for c in post_calls)
        # 配对成功消息应包含 user_id
        post_call = next(c for c in post_calls if "/message/custom/send" in c["url"])
        assert "user-1" in post_call["json"]["text"]["content"]

    def test_pairing_start_invalid_token(self, monkeypatch):
        # /start <无效 token> 应返回错误提示，不配对
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
            pairing_tokens={"abc123": "user-1"},
        )
        conn._running = True

        body = _make_xml_payload(content="/start wrong-token")
        ts = "1"
        nonce = "n"
        sig = _make_signature("mytoken", ts, nonce)

        asyncio.run(conn.handle_webhook(body, sig, ts, nonce))

        # 配对表不应记录
        assert "oABC123test" not in conn._paired
        # 应发送 token 无效提示
        post_calls = [c for c in mock_client.calls if c["method"] == "POST"]
        assert any("无效" in c["json"]["text"]["content"] for c in post_calls)


# =====================================================================
# 8. 退订命令
# =====================================================================


class TestUnsubscribe:
    """测试退订命令调 guard.record_unsubscribe"""

    def test_unsubscribe_command(self, monkeypatch):
        # 用户已配对，发"退订"应调 guard.record_unsubscribe
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        guard = MagicMock()
        guard.record_unsubscribe = MagicMock()

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
            guard=guard,
        )
        conn._running = True
        conn._paired["oABC123test"] = "user-1"

        for cmd in ("退订", "0", "STOP"):
            guard.record_unsubscribe.reset_mock()
            mock_client.calls.clear()

            body = _make_xml_payload(content=cmd)
            ts = "1"
            nonce = "n"
            sig = _make_signature("mytoken", ts, nonce)

            asyncio.run(conn.handle_webhook(body, sig, ts, nonce))

            # guard.record_unsubscribe 应被调用，scope="all"
            guard.record_unsubscribe.assert_called_once_with("user-1", scope="all")
            # 队列不应有消息（命令已处理）
            assert conn._queue.empty()


# =====================================================================
# 9. /help 命令
# =====================================================================


class TestHelpCommand:
    """测试 /help 命令返回帮助文本"""

    def test_help_command(self, monkeypatch):
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
        )
        conn._running = True
        conn._paired["oABC123test"] = "user-1"

        body = _make_xml_payload(content="/help")
        ts = "1"
        nonce = "n"
        sig = _make_signature("mytoken", ts, nonce)

        asyncio.run(conn.handle_webhook(body, sig, ts, nonce))

        # 应发送帮助文本（含 /start / 退订 / /help 三个命令说明）
        post_calls = [c for c in mock_client.calls if c["method"] == "POST"]
        assert any("/help" in c["json"]["text"]["content"] for c in post_calls)
        help_call = next(c for c in post_calls if "/help" in c["json"]["text"]["content"])
        assert "退订" in help_call["json"]["text"]["content"]
        # 队列应为空（命令已处理）
        assert conn._queue.empty()


# =====================================================================
# 10-11. 无 app_id/app_secret 时优雅降级
# =====================================================================


class TestGracefulDegradation:
    """无 app_id/app_secret 时优雅降级"""

    def test_no_app_id_start_does_not_raise(self):
        # 无 app_id 时 start() 不抛异常
        conn = WeChatConnector(app_id="", app_secret="", verify_token="mytoken")
        # 应静默返回，不抛异常
        asyncio.run(conn.start())
        assert conn._running is False  # 未启动

    def test_no_app_id_handle_webhook_returns_success(self):
        # 无 app_id 时 handle_webhook 返回 b"success"
        conn = WeChatConnector(app_id="", app_secret="", verify_token="mytoken")
        resp = asyncio.run(conn.handle_webhook(b"<xml>anything</xml>", "", "", ""))
        assert resp == b"success"

    def test_no_app_id_send_returns_false(self):
        # 无 app_id 时 send() 返回 False
        conn = WeChatConnector(app_id="", app_secret="", verify_token="mytoken")
        ok = asyncio.run(conn.send("user-1", "你好"))
        assert ok is False


# =====================================================================
# 12. send 调用正确的 URL
# =====================================================================


class TestSendCallsCorrectURL:
    """send() 应调用正确的 URL 与 body"""

    def test_send_calls_correct_url_and_body(self, monkeypatch):
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
        )
        # 先配对 user-1 → openid
        conn._paired["oABC123test"] = "user-1"
        conn._user_to_openid["user-1"] = "oABC123test"

        ok = asyncio.run(conn.send("user-1", "你好世界"))
        assert ok is True

        # 应有 token 获取的 GET 与 send 的 POST 两次调用
        get_calls = [c for c in mock_client.calls if c["method"] == "GET"]
        post_calls = [c for c in mock_client.calls if c["method"] == "POST"]

        # 1. token 获取 URL
        token_call = next(c for c in get_calls if "/token" in c["url"])
        assert token_call["params"]["grant_type"] == "client_credential"
        assert token_call["params"]["appid"] == "wx_app"
        assert token_call["params"]["secret"] == "secret"

        # 2. message/custom/send URL
        send_call = next(c for c in post_calls if "/message/custom/send" in c["url"])
        # params 应含 access_token
        assert send_call["params"]["access_token"] == "test-token"
        # body 应含正确的 touser / msgtype / text.content
        body = send_call["json"]
        assert body["touser"] == "oABC123test"
        assert body["msgtype"] == "text"
        assert body["text"]["content"] == "你好世界"

    def test_send_unpaired_user_fails(self, monkeypatch):
        # 未配对用户 send 失败
        mock_client = _MockAsyncClient()
        _patch_httpx(monkeypatch, mock_client)

        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
        )
        # 未配对
        ok = asyncio.run(conn.send("unknown-user", "你好"))
        assert ok is False


# =====================================================================
# 13. poll 拉取已注入消息
# =====================================================================


class TestPollYieldsMessages:
    """poll() 应从内部 queue 拉取已注入的消息"""

    def test_poll_yields_injected_messages(self):
        conn = WeChatConnector(
            app_id="wx_app",
            app_secret="secret",
            verify_token="mytoken",
        )
        conn._running = True

        # 注入 2 条消息
        conn._queue.put_nowait(("user-1", "消息1"))
        conn._queue.put_nowait(("user-1", "消息2"))

        results: list[tuple[str, str]] = []

        async def consume():
            async for user_id, text in conn.poll():
                results.append((user_id, text))
                if len(results) >= 2:
                    conn._running = False  # 让 poll 退出

        asyncio.run(asyncio.wait_for(consume(), timeout=2.0))

        assert len(results) == 2
        assert results[0] == ("user-1", "消息1")
        assert results[1] == ("user-1", "消息2")

    def test_poll_no_app_id_returns_empty(self):
        # 无 app_id 时 poll() 不 yield 任何消息
        conn = WeChatConnector(app_id="", app_secret="", verify_token="mytoken")

        results: list[tuple[str, str]] = []

        async def consume():
            async for user_id, text in conn.poll():
                results.append((user_id, text))

        # 应立即返回（不阻塞）
        asyncio.run(asyncio.wait_for(consume(), timeout=1.0))
        assert results == []
