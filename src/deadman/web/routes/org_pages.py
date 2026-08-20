"""机构工作台页面 + 聚合端点（B2B-IMPLEMENTATION Step 6）

页面路由：
  GET /org                → 返回 org.html（SPA 工作台）

聚合端点（对应工作台左侧导航面板）：
  GET /api/org/dashboard  → 客户数/进行中案件/我的待办/到期提醒/团队负载
  GET /api/org/members    → 成员列表（org_admin）
  GET /api/org/audit      → 审计日志（org_admin，案件事件倒序）
  GET /api/org/kb         → 平台公共库 + 机构私有库合并视图（机构成员）
  POST/PATCH/DELETE /api/org/kb/{doc_id} → 机构私有知识 CRUD（case_manager+）

权限对齐 B2B-TECH-DESIGN §6：
  - dashboard：org.view（viewer+）
  - members / audit：org.members.manage / org.audit.view（org_admin）
  - kb：读取 viewer+；编辑 case_manager+
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ...config import settings
from ...research.org_doc_rag import OrgDocRag
from ..deps import (
    get_case_event_repo,
    get_case_repo,
    get_customer_repo,
    get_org_store,
    require_org_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["org-pages"])

_STATIC_DIR = Path(__file__).parent.parent / "static"


# =====================================================================
# 请求模型
# =====================================================================
class KbDoc(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(default="民俗", max_length=40)
    content: str = ""
    tags: list[str] = []


# =====================================================================
# 页面路由
# =====================================================================
@router.get("/org", include_in_schema=False)
async def org_index() -> FileResponse:
    """机构工作台入口页（org.html）。"""
    return FileResponse(
        _STATIC_DIR / "org.html", media_type="text/html; charset=utf-8"
    )


# =====================================================================
# 仪表盘聚合
# =====================================================================
_ACTIVE_STATUSES = {"created", "assigned", "in_progress", "pending_input"}


@router.get("/api/org/dashboard")
async def org_dashboard(ctx: dict = Depends(require_org_role("viewer"))):
    """机构仪表盘聚合数据（viewer+）。"""
    tenant_id = ctx["tenant_id"]
    customer_repo = get_customer_repo()
    case_repo = get_case_repo()

    customer_count = await customer_repo.count_by_org(tenant_id)
    cases = await case_repo.list_by_org(tenant_id)

    active = [c for c in cases if c.get("status") in _ACTIVE_STATUSES]
    mine = [
        c
        for c in active
        if c.get("assignee_user_id") == ctx["user_id"]
    ]
    due_soon = [c for c in active if c.get("priority") == "high"]

    # 团队负载：按负责人聚合进行中案件
    load: dict[str, int] = {}
    for c in active:
        uid = c.get("assignee_user_id")
        if uid:
            load[uid] = load.get(uid, 0) + 1

    # 案件状态分布（供图表）
    status_breakdown: dict[str, int] = {}
    for c in cases:
        s = c.get("status", "")
        status_breakdown[s] = status_breakdown.get(s, 0) + 1

    recent = sorted(cases, key=lambda c: c.get("updated_at", ""), reverse=True)[:8]
    return {
        "customer_count": customer_count,
        "case_count": len(cases),
        "active_count": len(active),
        "my_todos": len(mine),
        "due_soon": len(due_soon),
        "team_load": load,
        "status_breakdown": status_breakdown,
        "recent_cases": recent,
    }


# =====================================================================
# 成员列表
# =====================================================================
@router.get("/api/org/members")
async def org_members(ctx: dict = Depends(require_org_role("org_admin"))):
    """机构成员列表（org_admin，附成员姓名）。"""
    from ..deps import get_user_store

    org_store = get_org_store()
    user_store = get_user_store()
    members = org_store.list_members(ctx["tenant_id"])
    rows = []
    for m in members:
        d = m.to_dict()
        user = user_store.get_user(d.get("user_id", ""))
        d["display_name"] = (user or {}).get("display_name", "") or d.get(
            "user_id", ""
        )
        rows.append(d)
    return {"members": rows, "count": len(rows)}


# =====================================================================
# 审计日志
# =====================================================================
@router.get("/api/org/audit")
async def org_audit(ctx: dict = Depends(require_org_role("org_admin"))):
    """审计日志（org_admin）：各案件事件倒序合并，最多 200 条。"""
    tenant_id = ctx["tenant_id"]
    case_repo = get_case_repo()
    event_repo = get_case_event_repo()
    cases = await case_repo.list_by_org(tenant_id)
    events: list[dict[str, Any]] = []
    for c in cases[:200]:
        evts = await event_repo.list_by_case(tenant_id, c["id"])
        for e in evts:
            e = dict(e)
            e["case_type"] = c.get("case_type", "")
            events.append(e)
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return {"events": events[:200], "count": len(events)}


# =====================================================================
# 机构知识库（平台公共库 + 机构私有库合并视图）
# =====================================================================
class _OrgKb:
    """机构私有知识 JSON 存储（org_data_dir/kb.json，按 org_id 隔离）。"""

    def __init__(self) -> None:
        self.path: Path = settings.org_data_dir / "kb.json"
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list_by_org(self, org_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(doc)
                for doc in self._load().values()
                if doc.get("org_id") == org_id
            ]

    def get(self, org_id: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._load().get(doc_id)
            if doc is not None and doc.get("org_id") == org_id:
                return dict(doc)
            return None

    def upsert(
        self, org_id: str, doc_id: str, data: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        doc = {
            "id": doc_id,
            "org_id": org_id,
            "title": data["title"],
            "category": data.get("category", "民俗"),
            "content": data.get("content", ""),
            "tags": list(data.get("tags", []) or []),
            "created_by": actor,
            "updated_by": actor,
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }
        with self._lock:
            all_data = self._load()
            existing = all_data.get(doc_id)
            if existing and existing.get("org_id") != org_id:
                raise ValueError("文档不存在或不属于该机构")
            if existing is None:
                doc["created_at"] = doc["updated_at"]
            else:
                doc["created_at"] = existing.get("created_at", doc["updated_at"])
            all_data[doc_id] = doc
            self._save(all_data)
        return dict(doc)

    def delete(self, org_id: str, doc_id: str) -> bool:
        with self._lock:
            all_data = self._load()
            doc = all_data.get(doc_id)
            if doc is None or doc.get("org_id") != org_id:
                return False
            del all_data[doc_id]
            self._save(all_data)
            return True


_kb = _OrgKb()

_rag: OrgDocRag | None = None


def _get_rag() -> OrgDocRag:
    """机构文档 RAG（懒加载单例）。"""
    global _rag
    if _rag is None:
        _rag = OrgDocRag()
    return _rag


def _platform_kb() -> list[dict[str, Any]]:
    """平台公共库：src/knowledge/regions/ 下的政策文档（只读）。"""
    docs = []
    kdir = settings.knowledge_dir / "regions"
    if kdir.exists():
        for f in sorted(kdir.rglob("*.md")):
            rel = f.relative_to(settings.project_root).as_posix()
            name = f.stem
            if name in ("overview", "SCHEMA"):
                continue
            docs.append(
                {
                    "id": rel,
                    "title": name,
                    "category": "平台政策",
                    "source": "public",
                    "path": rel,
                    "size": f.stat().st_size,
                }
            )
    return docs


@router.get("/api/org/kb")
async def org_kb(ctx: dict = Depends(require_org_role("viewer"))):
    """知识库合并视图：平台公共库（只读）+ 机构私有库（可编辑）。"""
    tenant_id = ctx["tenant_id"]
    private = sorted(_kb.list_by_org(tenant_id), key=lambda d: d.get("title", ""))
    return {
        "platform": _platform_kb(),
        "private": private,
        "platform_count": len(_platform_kb()),
        "private_count": len(private),
    }


@router.post("/api/org/kb/{doc_id}")
async def org_kb_upsert(
    doc_id: str,
    req: KbDoc,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """创建/更新机构私有知识（case_manager+）。同步写入 RAG 索引。"""
    try:
        doc = _kb.upsert(ctx["tenant_id"], doc_id, req.model_dump(), ctx["user_id"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    # 同步索引到机构 RAG（供「机构文档问答」检索）
    _get_rag().index_document(ctx["tenant_id"], doc_id, req.title, req.content or "")
    return doc


@router.delete("/api/org/kb/{doc_id}")
async def org_kb_delete(
    doc_id: str,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """删除机构私有知识（case_manager+）。同步移除 RAG 索引。"""
    if not _kb.delete(ctx["tenant_id"], doc_id):
        raise HTTPException(status_code=404, detail="文档不存在或不属于该机构")
    _get_rag().delete_document(ctx["tenant_id"], doc_id)
    return {"deleted": True, "doc_id": doc_id}


@router.get("/api/org/kb/query")
async def org_kb_query(
    q: str = Query(default=""),
    top_k: int = Query(default=5, ge=1, le=20),
    ctx: dict = Depends(require_org_role("viewer")),
):
    """机构文档 RAG 问答检索：在机构自建知识上检索 Top-k 块（viewer+ 只读）。"""
    if not q:
        return {"results": [], "count": 0}
    from ...research.query_rewrite import rewrite_query

    rewritten, was_rewritten = await rewrite_query(q)
    results = _get_rag().query(ctx["tenant_id"], rewritten, top_k=top_k)
    return {
        "query": q,
        "rewritten_query": rewritten if was_rewritten else None,
        "results": results,
        "count": len(results),
    }
