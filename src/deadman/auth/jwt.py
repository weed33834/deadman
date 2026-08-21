"""JWT 会话管理 - 基于 PyJWT 库。

Payload: {user_id, email, role, family_id, tenant_id, org_role, iat, exp}
过期时间默认 7 天，可配置
刷新阈值：剩余有效期小于 1 天时刷新

机构上下文（To B）：
  - tenant_id: 当前机构 id（未绑定为 None）
  - org_role: 机构内角色（viewer/consultant/case_manager/org_admin，未绑定为 None）
  平台层 role 与机构内 org_role 双轨，互不影响。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from pathlib import Path
from typing import Any

import jwt

from ..infrastructure.multi_tenant import DATA_ROOT

# 刷新阈值：剩余有效期小于 1 天时刷新
_REFRESH_THRESHOLD_SECONDS = 24 * 3600
_DEFAULT_SECRET_FILE = DATA_ROOT / "auth" / "jwt_secret"


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

    def issue(
        self,
        user: dict,
        tenant_id: str | None = None,
        org_role: str | None = None,
    ) -> str:
        """签发 JWT。user dict 至少包含：user_id, email, role。

        Args:
            tenant_id: 机构 id（To B；None = C 端/未绑定机构）
            org_role: 机构内角色（To B；与平台层 role 双轨）
            两者缺省时回退 user dict 中的同名键，保证 refresh(payload) 不丢机构上下文。
        """
        now = int(time.time())
        tenant_id = user.get("tenant_id") if tenant_id is None else tenant_id
        org_role = user.get("org_role") if org_role is None else org_role
        payload: dict[str, Any] = {
            "user_id": user.get("user_id"),
            "email": user.get("email"),
            "display_name": user.get("display_name"),
            "role": user.get("role", "user"),
            "family_id": user.get("family_id"),
            "tenant_id": tenant_id,
            "org_role": org_role,
            "iat": now,
            "exp": now + self.expiry_seconds,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def switch_org(self, user: dict, org_id: str, org_role: str) -> str:
        """切换当前机构：基于用户重签带 tenant_id/org_role 的 JWT。

        Args:
            user: 平台层用户 dict（来自 UserStore）
            org_id: 目标机构 id
            org_role: 该用户在此机构内的角色

        Returns:
            新 JWT（含机构上下文）
        """
        return self.issue(user, tenant_id=org_id, org_role=org_role)

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
