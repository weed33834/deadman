"""IAM —— 用户 / API Key / 权限矩阵（M11 缺口补齐）

把认证与授权管理暴露为管理接口：
  * GET    /api/admin/iam/users               —— 用户列表
  * PATCH  /api/admin/iam/users/{user_id}     —— 修改角色/显示名
  * GET    /api/admin/iam/keys                —— API Key 列表
  * POST   /api/admin/iam/keys                —— 生成 API Key
  * DELETE /api/admin/iam/keys/{key}          —— 吊销 API Key
  * GET    /api/admin/iam/permissions         —— 权限矩阵（模块 × 动作）

设计：复用 auth.UserStore（role 字段）；API Key 持久化 ~/.deadman/iam/keys.json。
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/iam", tags=["iam"])

# 角色
ROLES = ["admin", "developer", "tester", "viewer", "user"]
# 权限矩阵：模块 × 动作（查看/编辑/测试/启用/删除）
_PERMISSIONS = {
    "提示词": {
        "viewer": ["view"],
        "tester": ["view", "test"],
        "developer": ["view", "edit", "test"],
        "admin": ["view", "edit", "test", "enable", "delete"],
    },
    "Agent": {
        "viewer": ["view"],
        "developer": ["view", "edit", "test"],
        "admin": ["view", "edit", "test", "enable", "delete"],
    },
    "工具": {
        "viewer": ["view"],
        "tester": ["view", "test"],
        "developer": ["view", "test", "enable"],
        "admin": ["view", "test", "enable", "delete"],
    },
    "模型": {
        "viewer": ["view"],
        "developer": ["view", "edit"],
        "admin": ["view", "edit", "enable"],
    },
    "会话": {
        "user": ["view", "edit", "delete"],
        "viewer": ["view"],
        "admin": ["view", "edit", "delete"],
    },
    "备份": {"admin": ["view", "edit", "delete"]},
    "IAM": {"admin": ["view", "edit", "delete"]},
}


def _keys_dir() -> Path:
    d = Path.home() / ".deadman" / "iam"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_keys() -> dict[str, dict[str, Any]]:
    p = _keys_dir() / "keys.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_keys(data: dict[str, dict[str, Any]]) -> None:
    (_keys_dir() / "keys.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _user_store():
    from ...web.deps import get_user_store

    return get_user_store()


@router.get("/users")
async def iam_users() -> dict[str, Any]:
    """GET /api/admin/iam/users —— 用户列表"""
    try:
        users = _user_store().list_users()
        return {"ok": True, "users": users}
    except Exception as exc:
        raise DeadmanHTTPException("DM-INTERNAL-5000", message=f"用户列表失败: {exc}") from exc


@router.patch("/users/{user_id}")
async def iam_update_user(
    user_id: str,
    role: str = Body(default=None, embed=True, description="admin/developer/tester/viewer/user"),
    display_name: str = Body(default=None, embed=True),
) -> dict[str, Any]:
    """PATCH /api/admin/iam/users/{id} —— 修改角色/显示名"""
    updates: dict[str, Any] = {}
    if role is not None:
        if role not in ROLES:
            raise DeadmanHTTPException("DM-VALID-4001", message=f"role 仅支持 {ROLES}")
        updates["role"] = role
    if display_name is not None:
        updates["display_name"] = display_name
    if not updates:
        raise DeadmanHTTPException("DM-VALID-4002", message="至少提供一个字段")
    result = _user_store().update_user(user_id, updates)
    if result is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"用户不存在: {user_id}")
    return {"ok": True, "user": result}


@router.get("/keys")
async def iam_list_keys() -> dict[str, Any]:
    """GET /api/admin/iam/keys —— API Key 列表（隐藏完整 key）"""
    keys = _load_keys()
    view = [
        {
            "id": k,
            "masked": v.get("key", "")[:6] + "…",
            "label": v.get("label", ""),
            "created_at": v.get("created_at", ""),
            "last_used": v.get("last_used", ""),
        }
        for k, v in keys.items()
    ]
    return {"ok": True, "keys": view}


@router.post("/keys")
async def iam_create_key(
    label: str = Body(default="", embed=True, description="用途标签"),
) -> dict[str, Any]:
    """POST /api/admin/iam/keys —— 生成 API Key（仅显示一次）"""
    raw = secrets.token_urlsafe(24)
    kid = "key-" + secrets.token_hex(6)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    keys = _load_keys()
    keys[kid] = {"key": raw, "label": label or "默认", "created_at": now, "last_used": ""}
    _save_keys(keys)
    return {"ok": True, "key_id": kid, "api_key": raw, "note": "请立即保存，仅此一次显示"}


@router.delete("/keys/{key_id}")
async def iam_revoke_key(key_id: str) -> dict[str, Any]:
    """DELETE /api/admin/iam/keys/{id} —— 吊销 API Key"""
    keys = _load_keys()
    if key_id in keys:
        del keys[key_id]
        _save_keys(keys)
        return {"ok": True, "key_id": key_id, "revoked": True}
    raise DeadmanHTTPException("DM-GENERAL-4040", message=f"API Key 不存在: {key_id}")


@router.get("/permissions")
async def iam_permissions() -> dict[str, Any]:
    """GET /api/admin/iam/permissions —— 权限矩阵（模块 × 角色动作）"""
    return {"ok": True, "roles": ROLES, "matrix": _PERMISSIONS}
