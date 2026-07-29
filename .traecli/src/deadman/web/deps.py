"""FastAPI 依赖注入：认证、共享组件。

提供给 :mod:`deadman.web.app` 的可复用依赖：

* :func:`get_user_store` / :func:`get_jwt_manager` —— 懒加载 Phase 8 认证组件，
  遵循 ``web/server.py`` 的实例化方式（用 ``settings.auth_data_dir`` /
  ``settings.jwt_secret`` / ``settings.jwt_expiry_days``），便于测试 monkeypatch。
* :func:`get_current_user` —— 强制认证依赖，未登录或 token 无效时抛 401。
* :func:`get_optional_user` —— 可选认证依赖，未登录返回 ``None``（用于
  ``/api/chat`` / ``/api/stream`` 等允许匿名降级的端点）。

设计原则：
* 不修改 ``web/server.py`` —— 旧 stdlib http.server 保留为 fallback。
* 直接 import 现有业务模块，不重复造轮子。
"""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from ..auth.jwt import JWTManager
from ..auth.store import UserStore
from ..config import settings

__all__ = [
    "get_user_store",
    "get_jwt_manager",
    "get_current_user",
    "get_optional_user",
]


def get_user_store() -> UserStore:
    """懒加载 UserStore（用 settings.auth_data_dir，便于测试 monkeypatch）"""
    return UserStore(data_dir=settings.auth_data_dir)


def get_jwt_manager() -> JWTManager:
    """懒加载 JWTManager（与 web/server.py 同源）"""
    return JWTManager(
        secret=settings.jwt_secret or None,
        expiry_days=settings.jwt_expiry_days,
    )


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """从 ``Authorization: Bearer <token>`` 解析当前用户。

    未认证或 token 无效时抛 ``401``，与旧 ``_phase_unauthorized`` 行为一致。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未认证或 token 无效")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未认证或 token 无效")
    jwt_mgr = get_jwt_manager()
    payload = jwt_mgr.verify(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="未认证或 token 无效")
    store = get_user_store()
    user = store.get_user(payload.get("user_id", ""))
    if user is None:
        raise HTTPException(status_code=401, detail="未认证或 token 无效")
    return user


def get_optional_user(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
    """可选认证：未登录或 token 无效返回 ``None``，不报错。

    用于 ``/api/chat`` / ``/api/stream`` 等允许匿名降级的端点。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        return get_current_user(authorization)
    except HTTPException:
        return None
