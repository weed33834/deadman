"""密码重置功能测试（P1-3）

覆盖：
    - PasswordResetTokenStore: 令牌创建/消费/过期/单次使用
    - UserStore.find_user_by_email / update_password
    - API 端点：request（防枚举）/ confirm（成功/失败/过期/重放）
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# =====================================================================
# PasswordResetTokenStore 单元测试
# =====================================================================


class TestPasswordResetTokenStore:
    """令牌存储核心行为"""

    def test_create_and_consume_token_succeeds(self, tmp_path: Path):
        from deadman.auth.password_reset import PasswordResetTokenStore

        store = PasswordResetTokenStore(data_dir=tmp_path)
        token = store.create_token("user-123", "test@example.com")

        assert isinstance(token, str)
        assert len(token) >= 32  # secrets.token_urlsafe(32) ≈ 43 chars

        info = store.consume_token(token)
        assert info is not None
        assert info["user_id"] == "user-123"
        assert info["email"] == "test@example.com"

    def test_consume_token_is_single_use(self, tmp_path: Path):
        """令牌消费后立即失效，二次消费返回 None"""
        from deadman.auth.password_reset import PasswordResetTokenStore

        store = PasswordResetTokenStore(data_dir=tmp_path)
        token = store.create_token("user-1", "a@b.com")

        first = store.consume_token(token)
        assert first is not None

        second = store.consume_token(token)
        assert second is None  # 已消费

    def test_consume_invalid_token_returns_none(self, tmp_path: Path):
        from deadman.auth.password_reset import PasswordResetTokenStore

        store = PasswordResetTokenStore(data_dir=tmp_path)
        assert store.consume_token("nonexistent-token") is None
        assert store.consume_token("") is None

    def test_expired_token_returns_none(self, tmp_path: Path):
        """过期令牌消费时返回 None 并被清理"""
        from deadman.auth.password_reset import PasswordResetTokenStore

        store = PasswordResetTokenStore(data_dir=tmp_path, ttl_minutes=0)
        token = store.create_token("user-2", "x@y.com")
        # TTL=0 → 立刻过期
        time.sleep(0.01)  # 确保 now > expires_at
        assert store.consume_token(token) is None

    def test_peek_does_not_consume(self, tmp_path: Path):
        """peek_token 查看但不删除令牌"""
        from deadman.auth.password_reset import PasswordResetTokenStore

        store = PasswordResetTokenStore(data_dir=tmp_path)
        token = store.create_token("user-3", "p@q.com")

        info = store.peek_token(token)
        assert info is not None
        assert info["user_id"] == "user-3"

        # peek 后仍可消费
        consumed = store.consume_token(token)
        assert consumed is not None

    def test_purge_all_clears_tokens(self, tmp_path: Path):
        from deadman.auth.password_reset import PasswordResetTokenStore

        store = PasswordResetTokenStore(data_dir=tmp_path)
        store.create_token("u1", "a@b.com")
        store.create_token("u2", "c@d.com")
        assert store.purge_all() == 2
        assert store.purge_all() == 0

    def test_token_survives_process_restart(self, tmp_path: Path):
        """令牌持久化到文件，新实例可读取"""
        from deadman.auth.password_reset import PasswordResetTokenStore

        store1 = PasswordResetTokenStore(data_dir=tmp_path)
        token = store1.create_token("user-restart", "r@s.com")

        # 模拟进程重启：新实例指向同一目录
        store2 = PasswordResetTokenStore(data_dir=tmp_path)
        info = store2.consume_token(token)
        assert info is not None
        assert info["user_id"] == "user-restart"


# =====================================================================
# UserStore 密码重置支持方法
# =====================================================================


class TestUserStorePasswordReset:
    """UserStore.find_user_by_email / update_password"""

    def test_find_user_by_email_returns_user_with_id(self, tmp_path: Path):
        from deadman.auth.store import UserStore

        store = UserStore(data_dir=tmp_path)
        store.password_min_length = 8
        store.register("findme@example.com", "Password1!", "Find")

        user = store.find_user_by_email("findme@example.com")
        assert user is not None
        assert "user_id" in user
        assert user["email"] == "findme@example.com"

    def test_find_user_by_email_case_insensitive(self, tmp_path: Path):
        from deadman.auth.store import UserStore

        store = UserStore(data_dir=tmp_path)
        store.password_min_length = 8
        # register 内部会 normalize 为小写存储
        store.register("Mixed@Example.com", "Password1!", "Mixed")

        # 用不同大小写查询应能找到（内部 normalize 比对 HMAC）
        user = store.find_user_by_email("mixed@example.com")
        assert user is not None
        # 存储的 email 已被 normalize 为小写
        assert user["email"] == "mixed@example.com"

    def test_find_user_by_nonexistent_email_returns_none(self, tmp_path: Path):
        from deadman.auth.store import UserStore

        store = UserStore(data_dir=tmp_path)
        assert store.find_user_by_email("ghost@example.com") is None

    def test_update_password_changes_hash(self, tmp_path: Path):
        from deadman.auth.store import UserStore

        store = UserStore(data_dir=tmp_path)
        store.password_min_length = 8
        store.register("reset@example.com", "OldPassword1!", "Reset")

        user = store.find_user_by_email("reset@example.com")
        assert user is not None

        ok = store.update_password(user["user_id"], "NewPassword2!")
        assert ok is True

        # 旧密码登录失败
        assert store.verify("reset@example.com", "OldPassword1!") is None
        # 新密码登录成功
        verified = store.verify("reset@example.com", "NewPassword2!")
        assert verified is not None

    def test_update_password_rejects_short_password(self, tmp_path: Path):
        from deadman.auth.store import UserStore

        store = UserStore(data_dir=tmp_path)
        store.password_min_length = 8
        store.register("short@example.com", "Password1!", "Short")

        user = store.find_user_by_email("short@example.com")
        assert user is not None

        ok = store.update_password(user["user_id"], "short")
        assert ok is False

    def test_update_password_nonexistent_user_returns_false(self, tmp_path: Path):
        from deadman.auth.store import UserStore

        store = UserStore(data_dir=tmp_path)
        assert store.update_password("nonexistent-uuid", "NewPassword1!") is False


# =====================================================================
# API 端点测试
# =====================================================================


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离的测试环境"""
    from deadman.config import settings

    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "auth_data_dir", auth_dir)
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)

    fixed_jwt = "pw-reset-test-jwt-secret-fixed-32bytes-do-not-use"
    monkeypatch.setattr(settings, "jwt_secret", fixed_jwt)
    monkeypatch.setenv("DEADMAN_JWT_SECRET", fixed_jwt)

    # 禁用 SMTP → request 端点会返回 dev_reset_token 便于测试
    monkeypatch.delenv("DEADMAN_SMTP_HOST", raising=False)

    return tmp_path


@pytest.fixture
def client(isolated_env, patch_llm) -> TestClient:
    from deadman.web.app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client: TestClient, email: str, password: str = "Password1!") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": email.split("@")[0]},
    )
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    return r.json()["user_id"]


class TestPasswordResetAPI:
    """密码重置 API 端点"""

    def test_request_returns_success_for_existing_email(self, client: TestClient):
        """已注册邮箱请求重置 → 返回成功 + dev_reset_token（SMTP 未配置）"""
        _register(client, "reset-api@example.com", "OldPass1!")
        r = client.post(
            "/api/auth/password-reset/request",
            json={"email": "reset-api@example.com"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "message" in body
        # SMTP 未配置 → 返回 dev_reset_token
        assert "dev_reset_token" in body
        assert len(body["dev_reset_token"]) >= 32

    def test_request_returns_success_for_nonexistent_email(self, client: TestClient):
        """未注册邮箱请求重置 → 返回相同成功响应（防枚举），无 dev_reset_token"""
        r = client.post(
            "/api/auth/password-reset/request",
            json={"email": "ghost-nonexistent@example.com"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "message" in body
        # 未注册 → 不生成令牌 → 无 dev_reset_token
        assert "dev_reset_token" not in body

    def test_confirm_resets_password_successfully(self, client: TestClient):
        """完整重置流程：request → confirm → 用新密码登录"""
        _register(client, "full-flow@example.com", "OldPass1!")

        # 1. 请求重置
        r1 = client.post(
            "/api/auth/password-reset/request",
            json={"email": "full-flow@example.com"},
        )
        assert r1.status_code == 200
        token = r1.json()["dev_reset_token"]

        # 2. 确认重置
        r2 = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": "BrandNewPass2!"},
        )
        assert r2.status_code == 200
        assert r2.json()["success"] is True

        # 3. 旧密码登录失败
        r3 = client.post(
            "/api/auth/login",
            json={"email": "full-flow@example.com", "password": "OldPass1!"},
        )
        assert r3.status_code == 401

        # 4. 新密码登录成功
        r4 = client.post(
            "/api/auth/login",
            json={"email": "full-flow@example.com", "password": "BrandNewPass2!"},
        )
        assert r4.status_code == 200

    def test_confirm_with_invalid_token_returns_400(self, client: TestClient):
        """无效令牌 → 400"""
        r = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": "invalid-token-xxx", "new_password": "NewPass1!"},
        )
        assert r.status_code == 400

    def test_confirm_replay_attack_returns_400(self, client: TestClient):
        """令牌单次使用：同一令牌二次确认 → 400"""
        _register(client, "replay@example.com", "OldPass1!")

        r1 = client.post(
            "/api/auth/password-reset/request",
            json={"email": "replay@example.com"},
        )
        token = r1.json()["dev_reset_token"]

        # 第一次成功
        r2 = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": "FirstReset1!"},
        )
        assert r2.status_code == 200

        # 第二次失败（重放）
        r3 = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": "SecondReset2!"},
        )
        assert r3.status_code == 400

    def test_confirm_short_password_returns_422(self, client: TestClient):
        """新密码 < 8 位 → 422（Pydantic 校验）"""
        _register(client, "short-pw@example.com", "OldPass1!")

        r1 = client.post(
            "/api/auth/password-reset/request",
            json={"email": "short-pw@example.com"},
        )
        token = r1.json()["dev_reset_token"]

        r2 = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": "short"},
        )
        assert r2.status_code == 422

    def test_request_missing_email_returns_422(self, client: TestClient):
        """缺少 email 字段 → 422"""
        r = client.post("/api/auth/password-reset/request", json={})
        assert r.status_code == 422

    def test_confirm_missing_fields_returns_422(self, client: TestClient):
        """缺少 token / new_password → 422"""
        r = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": "x"},
        )
        assert r.status_code == 422
