"""JWT 会话管理 - 自实现（不引入 pyjwt 依赖）

用 HMAC-SHA256 签名（HS256），不引入 RSA 复杂度
Payload: {user_id, email, role, family_id, iat, exp}
过期时间默认 7 天，可配置

实现要点（不用 pyjwt）：
  - header: {"alg": "HS256", "typ": "JWT"} base64url 编码
  - payload: 上述字段 base64url 编码
  - signature: HMAC-SHA256(header + "." + payload, secret) base64url 编码
  - 验证：重新计算签名比对 + 检查 exp
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any


# 默认配置
_DEFAULT_EXPIRY_DAYS = 7
_DEFAULT_SECRET_FILE = Path.home() / ".deadman" / "auth" / "jwt_secret"
# 刷新阈值：剩余有效期小于 1 天时刷新
_REFRESH_THRESHOLD_SECONDS = 24 * 3600


class JWTManager:
    """JWT 会话管理 - 自实现（不引入 pyjwt 依赖）

    用 HMAC-SHA256 签名（HS256），不引入 RSA 复杂度
    Payload: {user_id, email, role, family_id, iat, exp}
    过期时间默认 7 天，可配置
    """

    def __init__(self, secret: str | None = None, expiry_days: int = 7):
        # secret 默认从 ~/.deadman/auth/jwt_secret 读取，不存在则生成
        # 优先级：构造参数 > 环境变量 DEADMAN_JWT_SECRET > 文件
        if secret:
            self._secret: bytes = secret.encode("utf-8")
        else:
            env_secret = os.getenv("DEADMAN_JWT_SECRET", "").strip()
            if env_secret:
                self._secret = env_secret.encode("utf-8")
            else:
                self._secret = self._load_or_create_secret()
        self.expiry_days: int = expiry_days
        self.expiry_seconds: int = expiry_days * 24 * 3600

    # ================================================================
    # 公开 API
    # ================================================================

    def issue(self, user: dict) -> str:
        """签发 JWT

        user dict 至少包含：user_id, email, role
        可选：family_id
        """
        now = int(time.time())
        payload: dict[str, Any] = {
            "user_id": user.get("user_id"),
            "email": user.get("email"),
            "role": user.get("role", "user"),
            "family_id": user.get("family_id"),
            "iat": now,
            "exp": now + self.expiry_seconds,
        }
        return self._encode(payload)

    def verify(self, token: str) -> dict | None:
        """验证 JWT，返回 payload 或 None

        验证步骤：
        1. token 格式（三段式）
        2. 签名（重新计算比对，使用 compare_digest 防时序攻击）
        3. exp 过期时间
        """
        if not token or not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts

        # 验证签名
        expected_signature = self._sign(f"{header_b64}.{payload_b64}")
        if not hmac.compare_digest(expected_signature, signature_b64):
            return None

        # 解码 payload
        payload = self._decode_json(payload_b64)
        if payload is None:
            return None

        # 检查 exp
        exp = payload.get("exp")
        if exp is None or not isinstance(exp, (int, float)):
            return None
        if time.time() >= exp:
            return None

        return payload

    def refresh(self, token: str) -> str | None:
        """刷新 token（剩余有效期 < 1 天时签发新 token）

        - 剩余 < 1 天：返回新 token
        - 剩余 > 1 天：返回 None（不刷新，让旧 token 继续用）
        - token 无效/过期：返回 None
        """
        payload = self.verify(token)
        if payload is None:
            return None
        exp = payload.get("exp", 0)
        remaining = exp - time.time()
        if remaining > _REFRESH_THRESHOLD_SECONDS:
            return None
        # 签发新 token
        return self.issue(payload)

    # ================================================================
    # 内部工具 - JWT 编解码
    # ================================================================

    def _encode(self, payload: dict) -> str:
        """编码为 JWT 字符串"""
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = self._encode_json(header)
        payload_b64 = self._encode_json(payload)
        signing_input = f"{header_b64}.{payload_b64}"
        signature_b64 = self._sign(signing_input)
        return f"{signing_input}.{signature_b64}"

    def _sign(self, signing_input: str) -> str:
        """对 signing_input 计算 HMAC-SHA256，返回 base64url"""
        sig = hmac.new(
            self._secret,
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return self._b64url_encode(sig)

    @staticmethod
    def _encode_json(obj: dict) -> str:
        """JSON 序列化后 base64url 编码（无 padding）"""
        raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return JWTManager._b64url_encode(raw)

    @staticmethod
    def _decode_json(b64: str) -> dict | None:
        """base64url 解码后 JSON 反序列化"""
        try:
            raw = JWTManager._b64url_decode(b64)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _b64url_encode(data: bytes) -> str:
        """base64url 编码（无 padding）"""
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64url_decode(s: str) -> bytes:
        """base64url 解码（自动补 padding）"""
        # 补齐 padding
        padding_needed = (-len(s)) % 4
        s_padded = s + ("=" * padding_needed)
        return base64.urlsafe_b64decode(s_padded.encode("ascii"))

    @staticmethod
    def _load_or_create_secret() -> bytes:
        """加载或生成 JWT secret

        存在 ~/.deadman/auth/jwt_secret（hex 格式）
        """
        secret_file = _DEFAULT_SECRET_FILE
        try:
            secret_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if secret_file.exists():
            try:
                content = secret_file.read_text(encoding="utf-8").strip()
                if content:
                    return content.encode("utf-8")
            except OSError:
                pass
        # 生成 32 字节随机密钥
        new_secret = secrets.token_bytes(32).hex()
        try:
            secret_file.write_text(new_secret, encoding="utf-8")
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return new_secret.encode("utf-8")
