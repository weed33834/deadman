"""会话管理 —— 多会话 / 历史

把对话从"单会话内存"升级为"多会话持久化"：
  * GET    /api/sessions              —— 会话列表
  * POST   /api/sessions              —— 新建会话
  * DELETE /api/sessions/{id}         —— 删除会话
  * GET    /api/sessions/{id}/messages —— 获取某会话消息
  * POST   /api/sessions/{id}/messages —— 追加消息

持久化：``~/.deadman/sessions/<id>.json``（每条消息 {role, content, ts}）
设计：轻量 JSON 文件存储；防御式读写；供前端侧边栏多会话切换。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _sessions_dir() -> Path:
    d = Path.home() / ".deadman" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(id: str) -> dict[str, Any] | None:
    p = _sessions_dir() / f"{id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save(id: str, data: dict[str, Any]) -> None:
    (_sessions_dir() / f"{id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _safe_id(id: str) -> str:
    return "".join(c for c in id if c.isalnum() or c in "-_") or "session"


@router.get("")
async def list_sessions() -> dict[str, Any]:
    """GET /api/sessions —— 会话列表（按更新时间倒序）"""
    items: list[dict[str, Any]] = []
    for p in sorted(_sessions_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append(
                {
                    "id": data.get("id", p.stem),
                    "title": data.get("title", "未命名会话"),
                    "message_count": len(data.get("messages", [])),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return {"ok": True, "sessions": items}


@router.post("")
async def create_session(
    title: str = Body(default="", embed=True, description="会话标题"),
) -> dict[str, Any]:
    """POST /api/sessions —— 新建会话"""
    sid = f"sess-{uuid.uuid4().hex[:12]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data = {
        "id": sid,
        "title": title or "未命名会话",
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }
    _save(sid, data)
    return {"ok": True, "session": data}


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    """DELETE /api/sessions/{id} —— 删除会话"""
    sid = _safe_id(session_id)
    p = _sessions_dir() / f"{sid}.json"
    if p.exists():
        p.unlink(missing_ok=True)
        return {"ok": True, "session_id": sid, "deleted": True}
    raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")


@router.get("/{session_id}/messages")
async def get_messages(session_id: str) -> dict[str, Any]:
    """GET /api/sessions/{id}/messages —— 获取会话消息"""
    sid = _safe_id(session_id)
    data = _load(sid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")
    return {"ok": True, "session_id": sid, "messages": data.get("messages", [])}


@router.post("/{session_id}/messages")
async def append_message(
    session_id: str,
    role: str = Body(default=None, embed=True, description="user / assistant"),
    content: str = Body(default="", embed=True, description="消息内容"),
) -> dict[str, Any]:
    """POST /api/sessions/{id}/messages —— 追加一条消息"""
    sid = _safe_id(session_id)
    data = _load(sid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")
    if role not in ("user", "assistant"):
        raise DeadmanHTTPException("DM-VALID-4002", message="role 仅支持 user/assistant")
    msg = {
        "role": role,
        "content": content or "",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data.setdefault("messages", []).append(msg)
    # 标题：第一条用户消息截断
    if data.get("title") in ("", "未命名会话") and role == "user":
        data["title"] = (content or "未命名会话")[:20]
    data["updated_at"] = msg["ts"]
    _save(sid, data)
    return {"ok": True, "session_id": sid, "message": msg, "title": data["title"]}
