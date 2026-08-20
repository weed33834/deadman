"""定时任务管理测试 —— G11（propose/confirm/list/cancel + 双重确认）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.scheduler as sch

    # 隔离数据目录
    def _sched():
        from deadman.cron.scheduler import CronScheduler

        return CronScheduler(data_dir=tmp_path / "cron")

    monkeypatch.setattr(sch, "_scheduler", _sched)
    from deadman.web.routes import scheduler as scheduler_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(scheduler_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestScheduler:
    def test_propose_confirm_list_cancel(self, client):
        r = client.post(
            "/api/scheduler/jobs",
            json={"schedule": "0 9 * * *", "content": "提醒", "user_id": "u1"},
        )
        assert r.status_code == 200
        body = r.json()
        # 提议可能返回 ok 或 needs_confirmation（双重确认制）
        assert body.get("ok") is True or body.get("needs_confirmation") is True
        # 提议后入暂存
        jobs = client.get("/api/scheduler/jobs?user_id=u1").json()["jobs"]
        assert len(jobs) == 1
        jid = jobs[0]["job_id"]
        # 确认（激活）
        confirm = client.post(f"/api/scheduler/jobs/{jid}/confirm?user_id=u1")
        assert confirm.status_code == 200
        # 取消
        cancel = client.delete(f"/api/scheduler/jobs/{jid}?user_id=u1")
        assert cancel.status_code == 200
        assert len(client.get("/api/scheduler/jobs?user_id=u1").json()["jobs"]) == 0

    def test_propose_missing_fields(self, client):
        r = client.post("/api/scheduler/jobs", json={"content": "x", "user_id": "u1"})
        assert r.status_code in (400, 422)
