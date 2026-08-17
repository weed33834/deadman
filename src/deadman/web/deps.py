"""FastAPI 依赖注入：认证、共享组件。

提供给 :mod:`deadman.web.app` 的可复用依赖：

* :func:`get_user_store` / :func:`get_jwt_manager` —— 懒加载 Phase 8 认证组件，
  遵循 ``web/server.py`` 的实例化方式（用 ``settings.auth_data_dir`` /
  ``settings.jwt_secret`` / ``settings.jwt_expiry_days``），便于测试 monkeypatch。
* :func:`get_current_user` —— 强制认证依赖，未登录或 token 无效时抛 401。
* :func:`get_optional_user` —— 可选认证依赖，未登录返回 ``None``（用于
  ``/api/chat`` / ``/api/stream`` 等允许匿名降级的端点）。
* :data:`bearer_scheme` —— FastAPI ``HTTPBearer`` 安全方案，使 OpenAPI 文档
  自动出现"Authorize"按钮，并在所有认证路由上显示锁标记。

设计原则：
* 不修改 ``web/server.py`` —— 旧 stdlib http.server 保留为 fallback。
* 直接 import 现有业务模块，不重复造轮子。
* 用 ``HTTPBearer(auto_error=False)`` 作为子依赖，由 FastAPI 自动生成
  OpenAPI securityScheme（``bearerAuth``），Swagger UI / ReDoc 可直接调试。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth.jwt import JWTManager
from ..auth.store import UserStore
from ..config import settings

__all__ = [
    "get_user_store",
    "get_jwt_manager",
    "get_current_user",
    "get_optional_user",
    "require_admin",
    "bearer_scheme",
]


# Bearer 安全方案：auto_error=False 让 get_optional_user 能降级到匿名。
# FastAPI 据此在 OpenAPI 中声明 securitySchemes.bearerAuth，
# /docs 页面出现"Authorize"按钮，认证路由显示锁标记。
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


def get_user_store() -> UserStore:
    """懒加载 UserStore（用 settings.auth_data_dir，便于测试 monkeypatch）"""
    return UserStore(data_dir=settings.auth_data_dir)


def get_jwt_manager() -> JWTManager:
    """懒加载 JWTManager（与 web/server.py 同源）"""
    return JWTManager(
        secret=settings.jwt_secret or None,
        expiry_days=settings.jwt_expiry_days,
    )


def _resolve_user(token: str | None) -> dict[str, Any] | None:
    """共享的用户解析逻辑：token 有效返回 user dict，否则 None。

    抽出此函数避免 ``get_current_user`` 与 ``get_optional_user`` 重复解析逻辑
    （原实现中二者各写一遍 bearer 前缀剥离 + verify + get_user）。
    """
    if not token:
        return None
    jwt_mgr = get_jwt_manager()
    payload = jwt_mgr.verify(token)
    if payload is None:
        return None
    store = get_user_store()
    return store.get_user(payload.get("user_id", ""))


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    """从 ``Authorization: Bearer <token>`` 解析当前用户。

    未认证或 token 无效时抛 ``401``（附 ``WWW-Authenticate: Bearer`` 头，
    符合 RFC 7235），与旧 ``_phase_unauthorized`` 行为一致。

    通过 :data:`bearer_scheme` 子依赖，FastAPI 自动在 OpenAPI 文档中标注
    本依赖所在路由需要 Bearer 认证。
    """
    token = cred.credentials if cred else None
    user = _resolve_user(token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="未认证或 token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_optional_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any] | None:
    """可选认证：未登录或 token 无效返回 ``None``，不报错。

    用于 ``/api/chat`` / ``/api/stream`` 等允许匿名降级的端点。
    """
    token = cred.credentials if cred else None
    return _resolve_user(token)


def require_admin(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    strict: bool = False,
) -> dict[str, Any]:
    """管理员级认证依赖：``/api/admin/*`` 全族端点必须挂载本依赖。

    两种通过方式（满足其一即可）：
    1. ``X-Admin-Token`` 头与 ``DEADMAN_ADMIN_TOKEN`` 严格相等
       （与 :meth:`deadman.mcp_server.server._check_admin_token` 语义一致）；
    2. 有效的 JWT 用户（``Authorization: Bearer <token>``）。``strict=True``
       时要求用户 ``is_admin`` 为真，否则仅要求已认证。

    未配置 ``DEADMAN_ADMIN_TOKEN`` 且无 JWT 时抛 401；提供错误的
    ``X-Admin-Token`` 时同样抛 401（防探测，不区分"未配置"与"错误"）。
    """
    if x_admin_token:
        admin_token = os.environ.get("DEADMAN_ADMIN_TOKEN", "")
        if admin_token and x_admin_token == admin_token:
            return {"role": "admin", "source": "admin_token"}
        raise HTTPException(
            status_code=401,
            detail="X-Admin-Token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = cred.credentials if cred else None
    user = _resolve_user(token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="未认证或 token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if strict and not user.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="需要管理员权限",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"role": "user", "source": "jwt", "user": user}
