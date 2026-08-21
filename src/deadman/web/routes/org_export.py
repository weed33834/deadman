"""机构数据导出（B2B-IMPLEMENTATION Step 7.2）

端点（均要求 org_admin）：
  GET  /api/org/audit-logs              → 审计日志（JSON，支持 filters 过滤）
  GET  /api/org/audit-logs/export       → 审计导出（?format=csv|json，CSV 可直接下载）
  POST /api/org/export                  → 全量导出（客户/案件/审计/知识库，打包 zip 落盘）
  GET  /api/org/export/status           → 全量导出进度查询（job_id）

隔离保证：所有查询双键（org_id）过滤，导出内容仅含本机构数据。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from ...config import settings
from ..deps import (
    get_case_event_repo,
    get_case_repo,
    get_customer_repo,
    require_org_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["org-export"])

# 全量导出 job 状态（进程内内存态，单机足够；重启后进度丢失但 zip 已落盘）
_EXPORT_JOBS: dict[str, dict[str, Any]] = {}
_EXPORT_LOCK = threading.Lock()
_EXPORT_DIR = settings.org_data_dir / "exports"


# =====================================================================
# 审计日志
# =====================================================================
async def _collect_audit_events(
    tenant_id: str, case_repo, event_repo, limit: int = 500
) -> list[dict[str, Any]]:
    """合并机构内全部案件事件（倒序，最多 limit 条），附 case_type 便于过滤。"""
    cases = await case_repo.list_by_org(tenant_id)
    events: list[dict[str, Any]] = []
    for c in cases[: limit + 50]:
        for e in await event_repo.list_by_case(tenant_id, c["id"]):
            e = dict(e)
            e["case_id"] = c.get("id", "")
            e["case_type"] = c.get("case_type", "")
            e["customer_id"] = c.get("customer_id", "")
            events.append(e)
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return events[:limit]


def _filter_events(
    events: list[dict[str, Any]], actor: str | None, action: str | None, case_id: str | None
) -> list[dict[str, Any]]:
    """按查询参数过滤审计事件。"""
    rows = events
    if actor:
        rows = [e for e in rows if e.get("actor_user_id") == actor]
    if action:
        rows = [e for e in rows if e.get("action") == action]
    if case_id:
        rows = [e for e in rows if e.get("case_id") == case_id]
    return rows


@router.get("/audit-logs")
async def org_audit_logs(
    actor: str | None = None,
    action: str | None = None,
    case_id: str | None = None,
    limit: int = 200,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """审计日志查询（org_admin）：支持 actor / action / case_id 过滤。"""
    event_repo = get_case_event_repo()
    case_repo = get_case_repo()
    events = await _collect_audit_events(ctx["tenant_id"], case_repo, event_repo, limit=limit)
    rows = _filter_events(events, actor, action, case_id)
    return {"events": rows, "count": len(rows)}


def _events_to_csv(events: list[dict[str, Any]]) -> bytes:
    """审计事件转 CSV（UTF-8 BOM，Excel 可直接打开）。"""
    fieldnames = [
        "created_at",
        "case_id",
        "case_type",
        "customer_id",
        "actor_user_id",
        "action",
        "detail",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for e in events:
        writer.writerow(
            {
                "created_at": e.get("created_at", ""),
                "case_id": e.get("case_id", ""),
                "case_type": e.get("case_type", ""),
                "customer_id": e.get("customer_id", ""),
                "actor_user_id": e.get("actor_user_id", ""),
                "action": e.get("action", ""),
                "detail": json.dumps(e.get("detail", {}), ensure_ascii=False),
            }
        )
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


@router.get("/audit-logs/export")
async def org_audit_logs_export(
    fmt: str = "csv",
    actor: str | None = None,
    action: str | None = None,
    case_id: str | None = None,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """审计导出（org_admin）：?format=csv|json，默认 csv 直接下载。"""
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format 仅支持 csv / json")
    event_repo = get_case_event_repo()
    case_repo = get_case_repo()
    events = await _collect_audit_events(ctx["tenant_id"], case_repo, event_repo, limit=5000)
    rows = _filter_events(events, actor, action, case_id)

    if fmt == "csv":
        return Response(
            content=_events_to_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="org_audit_{ctx["tenant_id"]}.csv"'
            },
        )
    return Response(
        content=json.dumps({"events": rows, "count": len(rows)}, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="org_audit_{ctx["tenant_id"]}.json"'
        },
    )


# =====================================================================
# 全量导出（异步打包 zip）
# =====================================================================
@router.post("/export")
async def org_export(ctx: dict = Depends(require_org_role("org_admin"))):
    """全量导出（org_admin）：客户/案件/审计/知识库 → zip，异步打包。"""
    tenant_id = ctx["tenant_id"]
    job_id = uuid.uuid4().hex[:12]
    with _EXPORT_LOCK:
        _EXPORT_JOBS[job_id] = {"status": "running", "progress": 0, "path": None, "error": None}

    threading.Thread(
        target=_run_export, args=(job_id, tenant_id, ctx["user_id"]), daemon=True
    ).start()
    return {"job_id": job_id, "status": "running"}


def _run_export(job_id: str, tenant_id: str, actor: str) -> None:
    """后台线程执行导出（同步 IO，避免阻塞事件循环）。"""
    import asyncio

    try:
        asyncio.run(_export_org_data(job_id, tenant_id, actor))
    except Exception as exc:
        logger.exception("全量导出失败 %s: %s", job_id, exc)
        with _EXPORT_LOCK:
            _EXPORT_JOBS[job_id]["status"] = "failed"
            _EXPORT_JOBS[job_id]["error"] = str(exc)


async def _export_org_data(job_id: str, tenant_id: str, actor: str) -> None:
    case_repo = get_case_repo()
    event_repo = get_case_event_repo()
    customer_repo = get_customer_repo()

    customers = await customer_repo.list_by_org(tenant_id)
    _set_progress(job_id, 0.2)
    cases = await case_repo.list_by_org(tenant_id)
    _set_progress(job_id, 0.4)

    events: list[dict[str, Any]] = []
    for i, c in enumerate(cases):
        for e in await event_repo.list_by_case(tenant_id, c["id"]):
            e = dict(e)
            e["case_id"] = c.get("id", "")
            events.append(e)
        _set_progress(job_id, 0.4 + 0.4 * (i + 1) / max(1, len(cases)))
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)

    # 机构私有知识库（文件存储）
    private_kb = _org_kb_list(tenant_id)

    payload = {
        "exported_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "org_id": tenant_id,
        "exported_by": actor,
        "customers": customers,
        "cases": cases,
        "audit_events": events,
        "knowledge": private_kb,
    }

    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _EXPORT_DIR / f"org_{tenant_id}_{job_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "org_export.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        zf.writestr("audit_log.csv", _events_to_csv(events))

    with _EXPORT_LOCK:
        job = _EXPORT_JOBS[job_id]
        job["status"] = "done"
        job["progress"] = 1.0
        job["path"] = str(zip_path)
    logger.info(
        "全量导出完成: %s (%d 客户, %d 案件, %d 事件)",
        job_id,
        len(customers),
        len(cases),
        len(events),
    )


def _org_kb_list(org_id: str) -> list[dict[str, Any]]:
    """读取机构私有知识库（org_data_dir/kb.json，按 org_id 隔离）。"""
    path: Path = settings.org_data_dir / "kb.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [
        dict(doc) for doc in data.values() if isinstance(doc, dict) and doc.get("org_id") == org_id
    ]


def _set_progress(job_id: str, progress: float) -> None:
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
        if job:
            job["progress"] = round(progress, 3)


@router.get("/export/status")
async def org_export_status(
    job_id: str,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """导出进度查询（org_admin）；done 时返回 zip 下载路径。"""
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="导出任务不存在（进程重启后进度已清空）")
        return {
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "error": job["error"],
            "download_url": f"/api/org/export/{job_id}/download" if job.get("path") else None,
        }


@router.get("/export/{job_id}/download")
async def org_export_download(
    job_id: str,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """下载导出 zip（org_admin，job 需已完成）。"""
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
        path = Path(job["path"]) if job and job.get("path") else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="导出文件不存在或未完成")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
    )
