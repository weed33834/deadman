"""G6 画布协作 —— 写作/编辑工作区

把对话产出的内容沉淀为可编辑的画布（Canvas），支持多块（文本/代码）+ AI 续写。
  * GET    /api/canvas              —— 画布列表
  * POST   /api/canvas              —— 新建画布
  * GET    /api/canvas/{id}         —— 画布详情
  * PUT    /api/canvas/{id}         —— 更新画布（标题 / blocks）
  * DELETE /api/canvas/{id}         —— 删除画布
  * POST   /api/canvas/{id}/ai      —— AI 续写 / 改写某块

持久化：~/.deadman/canvas/<id>.json
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

router = APIRouter(prefix="/api/canvas", tags=["canvas"])


def _canvas_dir() -> Path:
    d = Path.home() / ".deadman" / "canvas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(id: str) -> dict[str, Any] | None:
    p = _canvas_dir() / f"{id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save(id: str, data: dict[str, Any]) -> None:
    (_canvas_dir() / f"{id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _safe_id(id: str) -> str:
    return "".join(c for c in id if c.isalnum() or c in "-_") or "canvas"


@router.get("")
async def canvas_list() -> dict[str, Any]:
    """GET /api/canvas —— 画布列表（按更新时间倒序）"""
    items = []
    for p in sorted(_canvas_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append(
                {
                    "id": data.get("id", p.stem),
                    "title": data.get("title", "未命名画布"),
                    "block_count": len(data.get("blocks", [])),
                    "updated_at": data.get("updated_at", ""),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return {"ok": True, "canvases": items}


@router.post("")
async def canvas_create(title: str = Body(default="", embed=True)) -> dict[str, Any]:
    """POST /api/canvas —— 新建画布"""
    cid = f"canvas-{uuid.uuid4().hex[:12]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data = {
        "id": cid,
        "title": title or "未命名画布",
        "blocks": [],
        "created_at": now,
        "updated_at": now,
    }
    _save(cid, data)
    return {"ok": True, "canvas": data}


@router.get("/{canvas_id}")
async def canvas_get(canvas_id: str) -> dict[str, Any]:
    """GET /api/canvas/{id} —— 画布详情"""
    cid = _safe_id(canvas_id)
    data = await asyncio.to_thread(_load, cid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"画布不存在: {canvas_id}")
    return {"ok": True, "canvas": data}


@router.put("/{canvas_id}")
async def canvas_update(
    canvas_id: str,
    title: str = Body(default=None, embed=True),
    blocks: list[dict[str, Any]] = Body(default=None, embed=True),  # noqa: B008
) -> dict[str, Any]:
    """PUT /api/canvas/{id} —— 更新标题 / blocks"""
    cid = _safe_id(canvas_id)
    data = await asyncio.to_thread(_load, cid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"画布不存在: {canvas_id}")
    if title is not None:
        data["title"] = title
    if blocks is not None:
        data["blocks"] = blocks
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save(cid, data)
    return {"ok": True, "canvas": data}


@router.delete("/{canvas_id}")
async def canvas_delete(canvas_id: str) -> dict[str, Any]:
    """DELETE /api/canvas/{id} —— 删除画布"""
    cid = _safe_id(canvas_id)
    p = _canvas_dir() / f"{cid}.json"
    if p.exists():
        p.unlink(missing_ok=True)
        return {"ok": True, "canvas_id": cid, "deleted": True}
    raise DeadmanHTTPException("DM-GENERAL-4040", message=f"画布不存在: {canvas_id}")


@router.post("/{canvas_id}/ai")
async def canvas_ai(
    canvas_id: str,
    block_index: int = Body(default=None, embed=True),
    instruction: str = Body(default="续写", embed=True),
) -> dict[str, Any]:
    """POST /api/canvas/{id}/ai —— AI 续写 / 改写某块"""
    from ...llm import llm_client

    cid = _safe_id(canvas_id)
    data = await asyncio.to_thread(_load, cid)
    if data is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"画布不存在: {canvas_id}")
    blocks = data.get("blocks", [])
    if block_index is None or not (0 <= block_index < len(blocks)):
        raise DeadmanHTTPException("DM-VALID-4001", message="block_index 越界")
    current = blocks[block_index].get("content", "")
    prompt = f"请对以下内容执行「{instruction}」，只输出结果：\n\n{current}"
    try:
        text = await llm_client.chat([{"role": "user", "content": prompt}], temperature=0.4)
    except Exception as exc:
        raise DeadmanHTTPException("DM-PROMPT-5000", message=f"AI 续写失败: {exc}") from exc
    blocks[block_index]["content"] = text
    data["blocks"] = blocks
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save(cid, data)
    return {"ok": True, "block_index": block_index, "content": text}
