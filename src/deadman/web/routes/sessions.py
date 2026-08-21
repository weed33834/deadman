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
                    "group": data.get("group", ""),
                    "starred": bool(data.get("starred", False)),
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


# =====================================================================
# 会话搜索 + 分组（G2 / G1）
# =====================================================================


@router.get("/search")
async def search_sessions(
    q: str = "",
    group: str = "",
) -> dict[str, Any]:
    """GET /api/sessions/search?q=&group= —— 按标题/内容关键词搜索，可按分组过滤"""
    q = (q or "").strip().lower()
    group = (group or "").strip()
    results: list[dict[str, Any]] = []
    for p in _sessions_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if group and data.get("group", "") != group:
            continue
        if q:
            hay = (
                data.get("title", "")
                + " "
                + " ".join(m.get("content", "") for m in data.get("messages", []))
            ).lower()
            if q not in hay:
                continue
        results.append(
            {
                "id": data.get("id", p.stem),
                "title": data.get("title", "未命名会话"),
                "group": data.get("group", ""),
                "message_count": len(data.get("messages", [])),
                "updated_at": data.get("updated_at", ""),
            }
        )
    results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"ok": True, "sessions": results, "query": q, "group": group}


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    title: str = Body(default=None, embed=True, description="新标题"),
    group: str = Body(default=None, embed=True, description="分组/项目名"),
) -> dict[str, Any]:
    """PATCH /api/sessions/{id} —— 更新会话标题/分组"""
    sid = _safe_id(session_id)
    data = _load(sid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")
    if title is not None:
        data["title"] = title[:40]
    if group is not None:
        data["group"] = group
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save(sid, data)
    return {
        "ok": True,
        "session_id": sid,
        "title": data.get("title"),
        "group": data.get("group", ""),
    }


@router.get("/groups")
async def list_groups() -> dict[str, Any]:
    """GET /api/sessions/groups —— 会话分组列表（含每组会话数）"""
    counts: dict[str, int] = {}
    for p in _sessions_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        g = data.get("group", "") or "默认"
        counts[g] = counts.get(g, 0) + 1
    groups = [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return {"ok": True, "groups": groups}


# =====================================================================
# G3 会话收藏 + G4 会话分享
# =====================================================================


@router.post("/{session_id}/star")
async def star_session(
    session_id: str, starred: bool = Body(default=True, embed=True)
) -> dict[str, Any]:
    """POST /api/sessions/{id}/star —— 收藏/取消收藏会话"""
    sid = _safe_id(session_id)
    data = _load(sid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")
    data["starred"] = bool(starred)
    _save(sid, data)
    return {"ok": True, "session_id": sid, "starred": bool(starred)}


@router.get("/starred")
async def list_starred() -> dict[str, Any]:
    """GET /api/sessions/starred —— 收藏的会话"""
    out: list[dict[str, Any]] = []
    for p in _sessions_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("starred"):
            out.append(
                {
                    "id": data.get("id", p.stem),
                    "title": data.get("title", ""),
                    "updated_at": data.get("updated_at", ""),
                }
            )
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"ok": True, "sessions": out}


# 会话分享：token → session 快照
def _shares_dir() -> Path:
    d = Path.home() / ".deadman" / "shares"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/{session_id}/share")
async def share_session(session_id: str) -> dict[str, Any]:
    """POST /api/sessions/{id}/share —— 生成分享链接（只读快照）"""
    sid = _safe_id(session_id)
    data = _load(sid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")
    token = uuid.uuid4().hex[:16]
    share = {
        "token": token,
        "session_id": sid,
        "title": data.get("title", "未命名会话"),
        "messages": data.get("messages", []),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (_shares_dir() / f"{token}.json").write_text(
        json.dumps(share, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "token": token, "share_url": f"/share/{token}"}


@router.delete("/{session_id}/share")
async def unshare_session(session_id: str) -> dict[str, Any]:
    """DELETE /api/sessions/{id}/share —— 撤销该会话的所有分享"""
    sid = _safe_id(session_id)
    removed = 0
    for p in _shares_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("session_id") == sid:
            p.unlink(missing_ok=True)
            removed += 1
    return {"ok": True, "removed": removed}


@router.get("/share/{token}")
async def get_share(token: str) -> dict[str, Any]:
    """GET /api/sessions/share/{token} —— 读取分享的会话（公开只读）"""
    p = _shares_dir() / f"{_safe_id(token)}.json"
    if not p.exists():
        raise DeadmanHTTPException("DM-GENERAL-4040", message="分享链接无效或已撤销")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise DeadmanHTTPException("DM-GENERAL-4040", message="分享链接无效或已撤销") from None
    return {"ok": True, "title": data.get("title"), "messages": data.get("messages", [])}


# =====================================================================
# 会话导出为周报 / 复盘 模板
# =====================================================================


@router.get("/{session_id}/report")
async def session_report(session_id: str, type: str = "weekly") -> dict[str, Any]:
    """GET /api/sessions/{id}/report?type=weekly|review —— 把会话导出为周报/复盘 Markdown

    用于把一段对话沉淀为可交付的周报 / 复盘文档。
    """
    sid = _safe_id(session_id)
    data = _load(sid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"会话不存在: {session_id}")
    messages = data.get("messages", [])
    now = time.strftime("%Y-%m-%d", time.gmtime())
    title = data.get("title", "未命名会话")
    if type == "review":
        md = [
            f"# 对话复盘 —— {title}",
            "",
            f"> 导出时间：{now} · 消息数：{len(messages)}",
            "",
            "## 一、背景与目标",
            "> （自动摘录：第一条用户消息）",
            _first_user(messages),
            "",
            "## 二、对话摘要",
            "",
        ]
        md += _summary(messages)
        md += ["", "## 三、待办 / 结论", ""]
        md += _todos(messages)
    else:  # weekly 周报
        md = [
            f"# 本周工作总结 —— {title}",
            "",
            f"> 导出时间：{now} · 消息数：{len(messages)}",
            "",
            "## 一、本周完成事项",
            "",
        ]
        md += _summary(messages)
        md += ["", "## 二、主要结论", ""]
        md += _conclusions(messages)
        md += ["", "## 三、下一步计划", "", "- [ ] （待补充）", ""]
    return {"ok": True, "type": type, "markdown": "\n".join(md)}


def _first_user(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user":
            return m.get("content", "").strip()
    return "—"


def _summary(messages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for m in messages[-20:]:
        role = "问" if m.get("role") == "user" else "答"
        content = (m.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"- **{role}**：{content[:120]}")
    if not lines:
        lines.append("- （暂无对话内容）")
    return lines


def _conclusions(messages: list[dict[str, Any]]) -> list[str]:
    """摘取最后的助手消息作为结论"""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            return ["- " + (m.get("content") or "").strip().replace("\n", " ")[:200]]
    return ["- （暂无）"]


def _todos(messages: list[dict[str, Any]]) -> list[str]:
    out = []
    for m in messages:
        content = m.get("content", "")
        for kw in ("待办", "需要", "记得", "下一步"):
            if kw in content:
                out.append(f"- [ ] {content.strip().replace(chr(10), ' ')[:80]}")
                break
    return out[:10] or ["- （无明确待办）"]
