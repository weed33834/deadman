"""测试 deadman.auth.jwt - JWT 会话管理（基于 PyJWT）

覆盖点（6 个）：
  - test_issue_and_verify: 签发后验证成功
  - test_verify_expired_fails: exp 过期失败
  - test_verify_tampered_fails: 篡改 payload 失败
  - test_verify_wrong_secret_fails: 不同 secret 失败
  - test_refresh_when_near_expiry: 剩余 < 1 天时刷新成功
  - test_refresh_when_far_from_expiry_returns_none: 剩余 > 1 天不刷新

测试隔离：每个测试用独立 secret，不依赖 ~/.deadman/auth/jwt_secret
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt as pyjwt
from deadman.auth.jwt import JWTManager

# =====================================================================
# 1-4. 签发与验证
# =====================================================================


class TestIssueAndVerify:
    """签发与验证基础路径"""

    def test_issue_and_verify(self):
        # 签发后验证成功
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        user = {
            "user_id": "u-001",
            "email": "alice@example.com",
            "role": "user",
            "family_id": None,
        }
        token = mgr.issue(user)
        assert isinstance(token, str)
        # JWT 应是三段式 base64url.base64url.base64url
        parts = token.split(".")
        assert len(parts) == 3

        payload = mgr.verify(token)
        assert payload is not None
        assert payload["user_id"] == "u-001"
        assert payload["email"] == "alice@example.com"
        assert payload["role"] == "user"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] > payload["iat"]

    def test_verify_expired_fails(self):
        # exp 过期失败
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        # 手动构造已过期的 token：直接用 _encode
        now = int(time.time())
        expired_payload = {
            "user_id": "u-002",
            "email": "bob@example.com",
            "role": "user",
            "iat": now - 8 * 24 * 3600,
            "exp": now - 1,  # 1 秒前过期
        }
        token = pyjwt.encode(expired_payload, mgr._secret, algorithm="HS256")
        assert mgr.verify(token) is None

    def test_verify_tampered_fails(self):
        # 篡改 payload 失败（signature 不匹配）
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        user = {"user_id": "u-003", "email": "carol@example.com", "role": "user"}
        token = mgr.issue(user)
        # 篡改 payload 段（中间段）
        parts = token.split(".")
        # 把 payload 替换为另一个 base64url（user_id 改为 hacker）
        import base64
        import json
        tampered_payload = {
            "user_id": "hacker",
            "email": "carol@example.com",
            "role": "admin",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        raw = json.dumps(tampered_payload, separators=(",", ":")).encode("utf-8")
        tampered_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        tampered_token = f"{parts[0]}.{tampered_b64}.{parts[2]}"
        assert mgr.verify(tampered_token) is None

    def test_verify_wrong_secret_fails(self):
        # 不同 secret 失败
        mgr1 = JWTManager(secret="secret-aaa", expiry_days=7)
        mgr2 = JWTManager(secret="secret-bbb", expiry_days=7)
        user = {"user_id": "u-004", "email": "dave@example.com", "role": "user"}
        token = mgr1.issue(user)
        # 用不同 secret 验证应失败
        assert mgr2.verify(token) is None
        # 用原 secret 验证应成功
        assert mgr1.verify(token) is not None


# =====================================================================
# 5-6. 刷新逻辑
# =====================================================================


class TestRefresh:
    """刷新 token 逻辑 - 剩余 < 1 天才刷新"""

    def test_refresh_when_near_expiry(self):
        # 剩余 < 1 天时刷新成功，返回新 token
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        # 构造一个剩余 1 小时的 token
        now = int(time.time())
        near_expiry_payload = {
            "user_id": "u-005",
            "email": "eve@example.com",
            "role": "user",
            "family_id": None,
            "iat": now - 6 * 24 * 3600,  # 6 天前签发
            "exp": now + 3600,  # 1 小时后过期（剩余 < 1 天）
        }
        token = pyjwt.encode(near_expiry_payload, mgr._secret, algorithm="HS256")
        new_token = mgr.refresh(token)
        assert new_token is not None
        assert new_token != token
        # 新 token 应可验证
        new_payload = mgr.verify(new_token)
        assert new_payload is not None
        assert new_payload["user_id"] == "u-005"
        # 新 token 的 exp 应比旧 token 晚
        assert new_payload["exp"] > near_expiry_payload["exp"]

    def test_refresh_when_far_from_expiry_returns_none(self):
        # 剩余 > 1 天不刷新，返回 None
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        user = {"user_id": "u-006", "email": "frank@example.com", "role": "user"}
        token = mgr.issue(user)
        # 刚签发的 token 剩余 7 天 > 1 天，不应刷新
        new_token = mgr.refresh(token)
        assert new_token is None

    def test_refresh_expired_token_returns_none(self):
        # 过期 token 不应刷新
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        now = int(time.time())
        expired_payload = {
            "user_id": "u-007",
            "email": "grace@example.com",
            "role": "user",
            "iat": now - 8 * 24 * 3600,
            "exp": now - 1,
        }
        token = pyjwt.encode(expired_payload, mgr._secret, algorithm="HS256")
        assert mgr.refresh(token) is None

    def test_refresh_invalid_token_returns_none(self):
        # 无效 token 不应刷新
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        assert mgr.refresh("not.a.valid.token") is None
        assert mgr.refresh("") is None
        assert mgr.refresh("xxx") is None


# =====================================================================
# 额外：边界情况
# =====================================================================


class TestEdgeCases:
    """边界情况"""

    def test_verify_malformed_token_returns_none(self):
        mgr = JWTManager(secret="test-secret-key-12345", expiry_days=7)
        # 非三段式
        assert mgr.verify("onlyone") is None
        assert mgr.verify("a.b") is None
        assert mgr.verify("a.b.c.d") is None
        # 空 token
        assert mgr.verify("") is None
        assert mgr.verify(None) is None  # type: ignore[arg-type]

    def test_default_secret_file_persistence(self, tmp_path: Path, monkeypatch):
        # 默认 secret 从文件加载，重启后保持一致
        # 临时改变默认路径到 tmp_path
        import deadman.auth.jwt as jwt_module

        fake_secret_file = tmp_path / "jwt_secret"
        monkeypatch.setattr(jwt_module, "_DEFAULT_SECRET_FILE", fake_secret_file)

        mgr1 = JWTManager()  # 会生成新 secret 并写入文件
        secret1 = mgr1._secret
        # 同一文件再加载
        mgr2 = JWTManager()
        assert mgr2._secret == secret1, "重启后 secret 不一致"
