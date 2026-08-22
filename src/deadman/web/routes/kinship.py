"""亲属图谱 —— 家族成员 + 关系 + 图谱可视化数据

  * GET    /api/kinship         —— 图谱（成员 + 关系，含图结构 nodes/edges）
  * POST   /api/kinship/member  —— 新增成员
  * PUT    /api/kinship/member/{id} —— 更新成员
  * DELETE /api/kinship/member/{id} —— 删除成员（连带关系）
  * POST   /api/kinship/relation —— 新增关系（spouse/parent/child/sibling）
  * DELETE /api/kinship/relation —— 删除关系（from/to/type）
  * POST   /api/kinship/clear   —— 清空

持久化：~/.deadman/kinship.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kinship", tags=["kinship"])

_DIR = Path.home() / ".deadman" / "kinship"
_REL_TYPES = ("spouse", "parent", "child", "sibling")


def _load() -> dict[str, Any]:
    p = _DIR / "kinship.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"members": [], "relations": [], "egoid": ""}


def _save(data: dict[str, Any]) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    (_DIR / "kinship.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _graph(data: dict[str, Any]) -> dict[str, Any]:
    """构建可视化图结构 nodes/edges（供前端 SVG 渲染）"""
    nodes = [
        {
            "id": m["id"],
            "label": m.get("name", "?"),
            "gender": m.get("gender", "unknown"),
            "note": m.get("note", ""),
        }
        for m in data.get("members", [])
    ]
    edges = [
        {"source": r["from"], "target": r["to"], "type": r["type"]}
        for r in data.get("relations", [])
    ]
    return {"nodes": nodes, "edges": edges}


@router.get("")
async def kinship_get() -> dict[str, Any]:
    """GET /api/kinship —— 图谱数据（成员+关系+图结构）"""
    data = await asyncio.to_thread(_load)
    data["graph"] = _graph(data)
    return {"ok": True, **data}


@router.post("/member")
async def kinship_add_member(member: dict[str, Any] = Body(default=None)) -> dict[str, Any]:  # noqa: B008
    """POST /api/kinship/member —— 新增成员"""
    member = member or {}
    if not member.get("name"):
        raise DeadmanHTTPException("DM-VALID-4002", message="name 必填")
    data = await asyncio.to_thread(_load)
    mid = member.get("id") or f"m-{uuid.uuid4().hex[:8]}"
    data["members"].append(
        {
            "id": mid,
            "name": member["name"],
            "gender": member.get("gender", "unknown"),
            "note": member.get("note", ""),
            "birth": member.get("birth", ""),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    if not data.get("egoid"):
        data["egoid"] = mid
    _save(data)
    return {"ok": True, "member": data["members"][-1]}


@router.put("/member/{member_id}")
async def kinship_update_member(
    member_id: str,
    member: dict[str, Any] = Body(default=None),  # noqa: B008
) -> dict[str, Any]:
    """PUT /api/kinship/member/{id} —— 更新成员"""
    data = await asyncio.to_thread(_load)
    target = next((m for m in data["members"] if m["id"] == member_id), None)
    if target is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"成员不存在: {member_id}")
    member = member or {}
    for k in ("name", "gender", "note", "birth"):
        if k in member:
            target[k] = member[k]
    _save(data)
    return {"ok": True, "member": target}


@router.delete("/member/{member_id}")
async def kinship_delete_member(member_id: str) -> dict[str, Any]:
    """DELETE /api/kinship/member/{id} —— 删除成员（连带其关系）"""
    data = await asyncio.to_thread(_load)
    before = len(data["members"])
    data["members"] = [m for m in data["members"] if m["id"] != member_id]
    data["relations"] = [
        r for r in data["relations"] if r["from"] != member_id and r["to"] != member_id
    ]
    if data.get("egoid") == member_id:
        data["egoid"] = ""
    _save(data)
    return {
        "ok": True,
        "deleted": before != len(data["members"]),
        "members_remaining": len(data["members"]),
    }


@router.post("/relation")
async def kinship_add_relation(rel: dict[str, Any] = Body(default=None)) -> dict[str, Any]:  # noqa: B008
    """POST /api/kinship/relation —— 新增关系 {from,to,type}"""
    rel = rel or {}
    if not rel.get("from") or not rel.get("to") or rel.get("type") not in _REL_TYPES:
        raise DeadmanHTTPException("DM-VALID-4001", message=f"关系需 from/to，type ∈ {_REL_TYPES}")
    if rel["from"] == rel["to"]:
        raise DeadmanHTTPException("DM-VALID-4001", message="from 与 to 不能相同")
    data = await asyncio.to_thread(_load)
    for r in data["relations"]:
        if r["from"] == rel["from"] and r["to"] == rel["to"] and r["type"] == rel["type"]:
            raise DeadmanHTTPException("DM-VALID-4001", message="该关系已存在")
    data["relations"].append({"from": rel["from"], "to": rel["to"], "type": rel["type"]})
    _save(data)
    return {"ok": True, "relation": data["relations"][-1]}


@router.delete("/relation")
async def kinship_delete_relation(
    from_id: str = "", to_id: str = "", type: str = ""
) -> dict[str, Any]:
    """DELETE /api/kinship/relation?from=&to=&type= —— 删除关系"""
    data = await asyncio.to_thread(_load)
    before = len(data["relations"])
    data["relations"] = [
        r
        for r in data["relations"]
        if not (r["from"] == from_id and r["to"] == to_id and r["type"] == type)
    ]
    _save(data)
    return {"ok": True, "deleted": before != len(data["relations"])}


@router.post("/clear")
async def kinship_clear() -> dict[str, Any]:
    """POST /api/kinship/clear —— 清空图谱"""
    _save({"members": [], "relations": [], "egoid": ""})
    return {"ok": True, "cleared": True}
