"""To B 私有化授权码（B2B-IMPLEMENTATION Step 7.1）。

形态：
    - DEADMAN_LICENSE_KEY 环境变量，或挂载文件 /app/license.lic（内容即 token）。
    - token = base64url(payload_json) + "." + base64url(HMAC-SHA256(payload_json))
    - payload 含 exp（过期时间戳），verifier 校验签名 + 有效期。
    - 无码进入 30 天试用（trial）；过期机构只读（RO 由上层强制）。

签发端（厂商控制台）：DEADMAN_LICENSE_SECRET 恒定，离线签发。
校验端（私有化实例）：同 secret 配置后验证，secret 未知则自动进入试用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 试用期时长（秒）：30 天
TRIAL_DAYS = 30
TRIAL_SECONDS = TRIAL_DAYS * 24 * 3600

# 授权码挂载路径（Docker 私有化交付约定，见 B2B-TECH-DESIGN §9）
LICENSE_FILE = Path(os.environ.get("DEADMAN_LICENSE_FILE", "/app/license.lic"))

# 试用期起始时间标记（首启写入）
_TRIAL_MARKER = Path.home() / ".deadman" / ".trial_started"

# 签发密钥（仅厂商签发端必须；校验端缺省时视为未知 → 试用）
SECRET_ENV = "DEADMAN_LICENSE_SECRET"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_license(secret: str, payload: dict[str, Any]) -> str:
    """签发授权码：payload + HMAC-SHA256 签名，base64url 拼接。"""
    body = _b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_license(secret: str, token: str) -> dict[str, Any] | None:
    """校验授权码：签名 + 有效期。失败返回 None（不抛异常）。"""
    try:
        body, sig = token.strip().split(".", 1)
        expected = _b64url(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(_unb64url(body).decode("utf-8"))
        exp = payload.get("exp")
        if isinstance(exp, int | float) and exp < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


def _load_token() -> str | None:
    """读取授权码：优先环境变量，其次挂载文件。"""
    env_token = os.getenv("DEADMAN_LICENSE_KEY", "").strip()
    if env_token:
        return env_token
    try:
        if LICENSE_FILE.exists():
            content = LICENSE_FILE.read_text(encoding="utf-8").strip()
            if content:
                return content
    except OSError as exc:
        logger.warning("授权码文件读取失败: %s", exc)
    return None


def license_status() -> dict[str, Any]:
    """当前授权状态：trial（无码/未校验通过）/ licensed（有效期至 exp）/ expired。

    Returns:
        dict: {
            "status": "trial" | "licensed" | "expired",
            "licensee": str | None,   # 签发时的机构/单位名
            "plan": str | None,
            "expires_at": int | None, # epoch 秒
            "readonly": bool,         # 过期时只读
        }
    """
    token = _load_token()
    if token is None:
        return {"status": "trial", "readonly": False, "expires_at": None,
                "licensee": None, "plan": None}

    secret = os.getenv(SECRET_ENV, "")
    if not secret:
        logger.warning("授权码存在但缺少 %s，无法校验 → 按试用处理", SECRET_ENV)
        return {"status": "trial", "readonly": False, "expires_at": None,
                "licensee": None, "plan": None}

    payload = verify_license(secret, token)
    if payload is None:
        # 签名有效但已过期 → expired；签名非法/载荷损坏 → 按试用处理
        body = token.strip().split(".", 1)[0] if "." in token else ""
        if body:
            try:
                parsed = json.loads(_unb64url(body).decode("utf-8"))
                exp = parsed.get("exp")
                if isinstance(exp, int | float) and exp < time.time():
                    return {"status": "expired", "readonly": True, "expires_at": int(exp),
                            "licensee": parsed.get("licensee"), "plan": parsed.get("plan")}
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                pass
        logger.warning("授权码校验失败（签名不合法）→ 按试用处理")
        return {"status": "trial", "readonly": False, "expires_at": None,
                "licensee": None, "plan": None}

    exp = payload.get("exp")
    if isinstance(exp, int | float) and exp < time.time():
        return {"status": "expired", "readonly": True, "expires_at": int(exp),
                "licensee": payload.get("licensee"), "plan": payload.get("plan")}

    return {"status": "licensed", "readonly": False,
            "expires_at": int(exp) if isinstance(exp, int | float) else None,
            "licensee": payload.get("licensee"), "plan": payload.get("plan")}


def trial_remaining_days() -> int:
    """首次启动距试用过期的剩余天数（从第一次运行开始计）。"""
    marker = _TRIAL_MARKER
    try:
        if marker.exists():
            started = float(marker.read_text(encoding="utf-8").strip())
        else:
            started = time.time()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(started), encoding="utf-8")
    except (OSError, ValueError):
        return TRIAL_DAYS
    remaining = TRIAL_SECONDS - (time.time() - started)
    return max(0, int((remaining + 0.99) // 86400))
