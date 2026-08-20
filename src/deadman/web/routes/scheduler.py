"""定时任务管理 —— G11 缺口补齐

把 cron/scheduler 的能力暴露为管理 API（供管理台/对话使用）：
  * GET    /api/scheduler/jobs            —— 任务列表
  * POST   /api/scheduler/jobs            —— 提议任务（双重确认制，先入暂存）
  * POST   /api/scheduler/jobs/{id}/confirm —— 确认任务（真正激活）
  * DELETE /api/scheduler/jobs/{id}       —— 取消任务

遵循 scheduler 约束：每用户最多 5 条、最小间隔 24h、最长持续 30 天、双重确认。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def _scheduler():
    from ...cron.scheduler import CronScheduler

    return CronScheduler()


@router.get("/jobs")
async def list_jobs(user_id: str = "default") -> dict[str, Any]:
    """GET /api/scheduler/jobs —— 任务列表"""
    try:
        jobs = _scheduler().list_jobs(user_id)
        return {"ok": True, "jobs": [j.to_dict() for j in jobs]}
    except Exception as exc:
        raise DeadmanHTTPException("DM-INTERNAL-5000", message=f"任务列表失败: {exc}") from exc


@router.post("/jobs")
async def propose_job(
    schedule: str = Body(default=None, embed=True, description="cron 表达式（5 字段）"),
    content: str = Body(default=None, embed=True, description="任务内容"),
    user_id: str = Body(default="default"),
) -> dict[str, Any]:
    """POST /api/scheduler/jobs —— 提议任务（待确认）"""
    if not schedule or not content:
        raise DeadmanHTTPException("DM-VALID-4002", message="schedule 与 content 必填")
    try:
        return await _scheduler().propose_job(user_id, schedule, content)
    except Exception as exc:
        raise DeadmanHTTPException("DM-INTERNAL-5000", message=f"提议任务失败: {exc}") from exc


@router.post("/jobs/{job_id}/confirm")
async def confirm_job(job_id: str, user_id: str = "default") -> dict[str, Any]:
    """POST /api/scheduler/jobs/{id}/confirm —— 确认任务（激活）"""
    try:
        return await _scheduler().confirm_job(user_id, job_id)
    except Exception as exc:
        raise DeadmanHTTPException("DM-INTERNAL-5000", message=f"确认任务失败: {exc}") from exc


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, user_id: str = "default") -> dict[str, Any]:
    """DELETE /api/scheduler/jobs/{id} —— 取消任务"""
    try:
        ok = await _scheduler().cancel_job(user_id, job_id)
        return {"ok": ok, "job_id": job_id, "cancelled": ok}
    except Exception as exc:
        raise DeadmanHTTPException("DM-INTERNAL-5000", message=f"取消任务失败: {exc}") from exc
