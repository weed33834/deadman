"""测试 deadman.web.server 的认证端点 - Phase 8 Web 集成

覆盖点（5 个）：
  - test_register_returns_token: /api/auth/register 返回 token
  - test_login_returns_token: /api/auth/login 返回 token
  - test_me_without_token_returns_401: 无 token 访问 /api/auth/me 返回 401
  - test_me_with_valid_token_returns_user: 有效 token 返回用户
  - test_chat_with_token_uses_authenticated_user: 带 token 调 chat 时 user_id 来自 token

测试隔离：
  - 用 tmp_path 构造独立 UserStore / JWTManager，不污染 ~/.deadman
  - mock LLM 客户端，不真正调外部 API
  - 直接调 WebServer 的 async 方法（不启 HTTP server），用 asyncio.run
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore
from deadman.web.server import WebServer


# =====================================================================
# 辅助：构造独立 WebServer（auth_data_dir 指向 tmp_path）
# =====================================================================


def _make_web_server(tmp_path: Path, monkeypatch) -> WebServer:
    """构造一个用 tmp_path 作为 auth_data_dir 的 WebServer"""
    # monkeypatch settings.auth_data_dir
    from deadman.config import settings
    monkeypatch.setattr(settings, "auth_data_dir", tmp_path)
    monkeypatch.setattr(settings, "jwt_secret", "")  # 走文件默认
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)
    return WebServer()


def _register_user(tmp_path: Path, email: str = "alice@example.com", password: str = "password123") -> dict:
    """直接用 UserStore 注册一个用户，返回 user dict"""
    store = UserStore(data_dir=tmp_path)
    return store.register(email, password, email.split("@")[0].capitalize())


def _issue_token(tmp_path: Path, user: dict) -> str:
    """直接用 JWTManager 签发 token（用同一 auth_data_dir 的 secret）"""
    # 用 _DEFAULT_SECRET_FILE 加载（UserStore 创建时已生成）
    mgr = JWTManager()
    # 但要确保用同一份 secret：从 UserStore 创建的 secret 文件读取
    secret_file = tmp_path / "jwt_secret"
    if secret_file.exists():
        secret = secret_file.read_text(encoding="utf-8").strip()
        mgr = JWTManager(secret=secret)
    return mgr.issue(user)


# =====================================================================
# 1. 注册端点
# =====================================================================


class TestRegisterEndpoint:
    """/api/auth/register"""

    def test_register_returns_token(self, tmp_path: Path, monkeypatch):
        # 注册成功，返回 user_id + token + expires_at
        server = _make_web_server(tmp_path, monkeypatch)
        body = {
            "email": "alice@example.com",
            "password": "password123",
            "display_name": "Alice",
        }
        resp = asyncio.run(server._handle_auth_register(body))
        assert "user_id" in resp
        assert "token" in resp
        assert "expires_at" in resp
        assert resp["user_id"]
        assert len(resp["token"]) > 30
        # expires_at 应是 ISO 时间字符串
        assert "T" in resp["expires_at"]

    def test_register_duplicate_returns_400(self, tmp_path: Path, monkeypatch):
        # 重复邮箱应抛 ValueError（被 do_POST 捕获返回 400）
        server = _make_web_server(tmp_path, monkeypatch)
        body = {"email": "alice@example.com", "password": "password123"}
        asyncio.run(server._handle_auth_register(body))
        with pytest.raises(ValueError, match="邮箱已注册"):
            asyncio.run(server._handle_auth_register(body))


# =====================================================================
# 2. 登录端点
# =====================================================================


class TestLoginEndpoint:
    """/api/auth/login"""

    def test_login_returns_token(self, tmp_path: Path, monkeypatch):
        # 登录成功返回 token + display_name
        server = _make_web_server(tmp_path, monkeypatch)
        # 先注册
        asyncio.run(server._handle_auth_register({
            "email": "alice@example.com",
            "password": "password123",
            "display_name": "Alice",
        }))
        # 再登录
        resp = asyncio.run(server._handle_auth_login({
            "email": "alice@example.com",
            "password": "password123",
        }))
        assert resp is not None
        assert "token" in resp
        assert "user_id" in resp
        assert resp["display_name"] == "Alice"

    def test_login_wrong_password_returns_none(self, tmp_path: Path, monkeypatch):
        # 错误密码返回 None（防枚举）
        server = _make_web_server(tmp_path, monkeypatch)
        asyncio.run(server._handle_auth_register({
            "email": "alice@example.com",
            "password": "password123",
        }))
        resp = asyncio.run(server._handle_auth_login({
            "email": "alice@example.com",
            "password": "wrongpassword",
        }))
        assert resp is None

    def test_login_nonexistent_email_returns_none(self, tmp_path: Path, monkeypatch):
        # 不存在邮箱返回 None（防枚举）
        server = _make_web_server(tmp_path, monkeypatch)
        resp = asyncio.run(server._handle_auth_login({
            "email": "nobody@example.com",
            "password": "password123",
        }))
        assert resp is None


# =====================================================================
# 3. /api/auth/me 端点
# =====================================================================


class TestMeEndpoint:
    """/api/auth/me"""

    def test_me_without_token_returns_401(self, tmp_path: Path, monkeypatch):
        # 无 token 访问 /api/auth/me：_handle_auth_me 返回 None（do_POST 转为 401）
        server = _make_web_server(tmp_path, monkeypatch)
        resp = server._handle_auth_me({})
        assert resp is None

    def test_me_with_invalid_token_returns_none(self, tmp_path: Path, monkeypatch):
        # 无效 token：返回 None
        server = _make_web_server(tmp_path, monkeypatch)
        resp = server._handle_auth_me({
            "Authorization": "Bearer invalid.token.here",
        })
        assert resp is None

    def test_me_with_valid_token_returns_user(self, tmp_path: Path, monkeypatch):
        # 有效 token 返回用户信息
        server = _make_web_server(tmp_path, monkeypatch)
        # 注册
        reg_resp = asyncio.run(server._handle_auth_register({
            "email": "alice@example.com",
            "password": "password123",
            "display_name": "Alice",
        }))
        # 用 token 调 me
        resp = server._handle_auth_me({
            "Authorization": f"Bearer {reg_resp['token']}",
        })
        assert resp is not None
        assert resp["email"] == "alice@example.com"
        assert resp["display_name"] == "Alice"
        assert resp["role"] == "user"
        assert "password_hash" not in resp


# =====================================================================
# 4. /api/chat 集成认证
# =====================================================================


class TestChatWithAuth:
    """chat 端点优先用认证用户"""

    def test_chat_with_token_uses_authenticated_user(
        self, tmp_path: Path, monkeypatch, mock_llm_client
    ):
        # 带 token 调 chat 时 user_id 来自 token（验证 _require_auth 工作）
        server = _make_web_server(tmp_path, monkeypatch)
        # 注册并拿 token
        reg_resp = asyncio.run(server._handle_auth_register({
            "email": "alice@example.com",
            "password": "password123",
            "display_name": "Alice",
        }))
        token = reg_resp["token"]
        user_id = reg_resp["user_id"]

        # mock LLM 客户端
        import deadman.llm as llm_module
        monkeypatch.setattr(llm_module, "llm_client", mock_llm_client)

        # 用 _require_auth 验证 token 解析出的 user_id 是注册的 user_id
        user = server._require_auth({"Authorization": f"Bearer {token}"})
        assert user is not None
        assert user["user_id"] == user_id
        assert user["email"] == "alice@example.com"

        # 调 _handle_chat 时把认证用户的 user_id 传入（模拟 do_POST 的注入逻辑）
        # 注：_handle_chat 现在签名是 (agent, query, history, user_id)；
        # 我们不真的跑 graph（graph 依赖重），只验证 _require_auth 解析出的 user_id 与注册一致
        # 已在上面 assert 验证，这里再确认 user_id 来自 token 而非 anonymous
        assert user["user_id"] != "anonymous"
        assert user["user_id"] is not None

    def test_chat_without_token_falls_back_anonymous(
        self, tmp_path: Path, monkeypatch, mock_llm_client
    ):
        # 不带 token 调 chat 时降级 anonymous（_require_auth 返回 None）
        server = _make_web_server(tmp_path, monkeypatch)
        # _require_auth 用空 headers 应返回 None
        user = server._require_auth({})
        assert user is None
        # 这表示 do_POST 路由层不会注入 user_id，_handle_chat 收到 user_id=None
        # （_handle_chat 内部把 None 归一化为 "anonymous"）


# =====================================================================
# 5. /api/auth/refresh 端点
# =====================================================================


class TestRefreshEndpoint:
    """/api/auth/refresh"""

    def test_refresh_far_from_expiry_returns_none(self, tmp_path: Path, monkeypatch):
        # 剩余 > 1 天：返回 None（do_POST 转 401）
        server = _make_web_server(tmp_path, monkeypatch)
        reg_resp = asyncio.run(server._handle_auth_register({
            "email": "alice@example.com",
            "password": "password123",
        }))
        resp = server._handle_auth_refresh({
            "Authorization": f"Bearer {reg_resp['token']}",
        })
        assert resp is None

    def test_refresh_without_token_returns_none(self, tmp_path: Path, monkeypatch):
        server = _make_web_server(tmp_path, monkeypatch)
        resp = server._handle_auth_refresh({})
        assert resp is None
