"""JWT 会话管理 - 基于 PyJWT 库。

Payload: {user_id, email, role, family_id, iat, exp}
过期时间默认 7 天，可配置
刷新阈值：剩余有效期小于 1 天时刷新
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from pathlib import Path
from typing import Any

import jwt

# 刷新阈值：剩余有效期小于 1 天时刷新
_REFRESH_THRESHOLD_SECONDS = 24 * 3600
_DEFAULT_SECRET_FILE = Path.home() / ".deadman" / "auth" / "jwt_secret"


class JWTManager:
    """JWT 会话管理 - 基于 PyJWT（HS256 签名）。"""

    def __init__(self, secret: str | None = None, expiry_days: int = 7):
        if secret:
            self._secret: str = secret
        else:
            env_secret = os.getenv("DEADMAN_JWT_SECRET", "").strip()
            if env_secret:
                self._secret = env_secret
            else:
                self._secret = self._load_or_create_secret()
        self.expiry_days: int = expiry_days
        self.expiry_seconds: int = expiry_days * 24 * 3600

    def issue(self, user: dict) -> str:
        """签发 JWT。user dict 至少包含：user_id, email, role。"""
        now = int(time.time())
        payload: dict[str, Any] = {
            "user_id": user.get("user_id"),
            "email": user.get("email"),
            "role": user.get("role", "user"),
            "family_id": user.get("family_id"),
            "iat": now,
            "exp": now + self.expiry_seconds,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify(self, token: str) -> dict | None:
        """验证 JWT，返回 payload 或 None。"""
        if not token or not isinstance(token, str):
            return None
        try:
            return jwt.decode(token, self._secret, algorithms=["HS256"])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None

    def refresh(self, token: str) -> str | None:
        """刷新 token（剩余有效期 < 1 天时签发新 token）。"""
        payload = self.verify(token)
        if payload is None:
            return None
        exp = payload.get("exp", 0)
        remaining = exp - time.time()
        if remaining > _REFRESH_THRESHOLD_SECONDS:
            return None
        return self.issue(payload)

    @staticmethod
    def _load_or_create_secret() -> str:
        """加载或生成 JWT secret（存在 ~/.deadman/auth/jwt_secret）。"""
        secret_file = _DEFAULT_SECRET_FILE
        with contextlib.suppress(OSError):
            secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            try:
                content = secret_file.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except OSError:
                pass
        new_secret = secrets.token_bytes(32).hex()
        try:
            secret_file.write_text(new_secret, encoding="utf-8")
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return new_secret
