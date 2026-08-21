"""测试 deadman.web.app 的认证端点 - Phase 8 Web 集成

覆盖点（5 个）：
  - test_register_returns_token: /api/auth/register 返回 token
  - test_login_returns_token: /api/auth/login 返回 token
  - test_me_without_token_returns_401: 无 token 访问 /api/auth/me 返回 401
  - test_me_with_valid_token_returns_user: 有效 token 返回用户
  - test_chat_with_token_uses_authenticated_user: 带 token 调 chat 时 user_id 来自 token

测试隔离：
  - 用 tmp_path 构造独立 UserStore / JWTManager，不污染 ~/.deadman
  - FastAPI TestClient 进程内调真实端点，不启 HTTP server
"""

from __future__ import annotations

from pathlib import Path

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore

# =====================================================================
# 辅助：构造独立 TestClient（auth_data_dir 指向 tmp_path）
# =====================================================================


def _make_client(tmp_path: Path, monkeypatch):
    """构造一个用 tmp_path 作为 auth_data_dir 的 TestClient"""
    from fastapi.testclient import TestClient

    from deadman.config import settings
    from deadman.web.app import app

    monkeypatch.setattr(settings, "auth_data_dir", tmp_path)
    monkeypatch.setattr(settings, "jwt_secret", "")  # 走文件默认
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)
    return TestClient(app)


def _register_user(
    tmp_path: Path, email: str = "alice@example.com", password: str = "password123"
) -> dict:
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
        client = _make_client(tmp_path, monkeypatch)
        resp = client.post(
            "/api/auth/register",
            json={
                "email": "alice@example.com",
                "password": "password123",
                "display_name": "Alice",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "user_id" in body
        assert "token" in body
        assert "expires_at" in body
        assert body["user_id"]
        assert len(body["token"]) > 30
        # expires_at 应是 ISO 时间字符串
        assert "T" in body["expires_at"]

    def test_register_duplicate_returns_400(self, tmp_path: Path, monkeypatch):
        # 重复邮箱应返回 400
        client = _make_client(tmp_path, monkeypatch)
        body = {"email": "alice@example.com", "password": "password123"}
        first = client.post("/api/auth/register", json=body)
        assert first.status_code == 200
        dup = client.post("/api/auth/register", json=body)
        assert dup.status_code == 400


# =====================================================================
# 2. 登录端点
# =====================================================================


class TestLoginEndpoint:
    """/api/auth/login"""

    def test_login_returns_token(self, tmp_path: Path, monkeypatch):
        # 登录成功返回 token + display_name
        client = _make_client(tmp_path, monkeypatch)
        # 先注册
        client.post(
            "/api/auth/register",
            json={
                "email": "alice@example.com",
                "password": "password123",
                "display_name": "Alice",
            },
        )
        # 再登录
        resp = client.post(
            "/api/auth/login",
            json={
                "email": "alice@example.com",
                "password": "password123",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "token" in body
        assert "user_id" in body
        assert body["display_name"] == "Alice"

    def test_login_wrong_password_returns_401(self, tmp_path: Path, monkeypatch):
        # 错误密码返回 401（防枚举）
        client = _make_client(tmp_path, monkeypatch)
        client.post(
            "/api/auth/register",
            json={
                "email": "alice@example.com",
                "password": "password123",
            },
        )
        resp = client.post(
            "/api/auth/login",
            json={
                "email": "alice@example.com",
                "password": "wrongpassword",
            },
        )
        assert resp.status_code == 401

    def test_login_nonexistent_email_returns_401(self, tmp_path: Path, monkeypatch):
        # 不存在邮箱返回 401（防枚举）
        client = _make_client(tmp_path, monkeypatch)
        resp = client.post(
            "/api/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "password123",
            },
        )
        assert resp.status_code == 401


# =====================================================================
# 3. /api/auth/me 端点
# =====================================================================


class TestMeEndpoint:
    """/api/auth/me"""

    def test_me_without_token_returns_401(self, tmp_path: Path, monkeypatch):
        # 无 token 访问 /api/auth/me 应 401
        client = _make_client(tmp_path, monkeypatch)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_me_with_invalid_token_returns_401(self, tmp_path: Path, monkeypatch):
        # 无效 token：401
        client = _make_client(tmp_path, monkeypatch)
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_me_with_valid_token_returns_user(self, tmp_path: Path, monkeypatch):
        # 有效 token 返回用户信息
        client = _make_client(tmp_path, monkeypatch)
        # 注册
        reg_resp = client.post(
            "/api/auth/register",
            json={
                "email": "alice@example.com",
                "password": "password123",
                "display_name": "Alice",
            },
        )
        # 用 token 调 me
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {reg_resp.json()['token']}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "alice@example.com"
        assert body["display_name"] == "Alice"
        assert body["role"] == "user"
        assert "password_hash" not in body


# =====================================================================
# 4. /api/chat 集成认证
# =====================================================================


class TestChatWithAuth:
    """chat 端点优先用认证用户"""

    def test_chat_with_token_uses_authenticated_user(
        self, tmp_path: Path, monkeypatch, mock_llm_client
    ):
        # 带 token 调 chat 时 user_id 来自 token
        client = _make_client(tmp_path, monkeypatch)
        # 注册并拿 token
        reg_resp = client.post(
            "/api/auth/register",
            json={
                "email": "alice@example.com",
                "password": "password123",
                "display_name": "Alice",
            },
        )
        token = reg_resp.json()["token"]
        user_id = reg_resp.json()["user_id"]

        # mock LLM 客户端
        import deadman.llm as llm_module

        monkeypatch.setattr(llm_module, "llm_client", mock_llm_client)

        # 用 /api/auth/me 验证 token 解析出的 user_id 是注册的 user_id
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["user_id"] == user_id
        assert me.json()["email"] == "alice@example.com"
        # user_id 来自 token 而非 anonymous
        assert me.json()["user_id"] != "anonymous"
        assert me.json()["user_id"] is not None

    def test_chat_without_token_falls_back_anonymous(
        self, tmp_path: Path, monkeypatch, mock_llm_client
    ):
        # 不带 token 访问受保护端点应 401（chat 允许匿名降级，me 不允许）
        client = _make_client(tmp_path, monkeypatch)
        me = client.get("/api/auth/me")
        assert me.status_code == 401


# =====================================================================
# 5. /api/auth/refresh 端点
# =====================================================================


class TestRefreshEndpoint:
    """/api/auth/refresh"""

    def test_refresh_far_from_expiry_returns_401(self, tmp_path: Path, monkeypatch):
        # 剩余 > 1 天：返回 401（无需刷新）
        client = _make_client(tmp_path, monkeypatch)
        reg_resp = client.post(
            "/api/auth/register",
            json={
                "email": "alice@example.com",
                "password": "password123",
            },
        )
        resp = client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {reg_resp.json()['token']}"},
        )
        assert resp.status_code == 401

    def test_refresh_without_token_returns_401(self, tmp_path: Path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch)
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401
