"""IAM + 提示词版本 + Trace 测试（独立 FastAPI 实例隔离）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.iam as iam
    import deadman.web.routes.resources as res

    monkeypatch.setattr(res, "_ADMIN_DIR", tmp_path)
    for name in ("_prompts_store", "_agents_store", "_voices_store", "_tool_runs_store"):
        monkeypatch.setattr(res, name, res._JsonStore(name.replace("_store", "") + ".json"))
    monkeypatch.setattr(
        iam,
        "_keys_dir",
        lambda: (lambda d: (d.mkdir(parents=True, exist_ok=True), d)[1])(tmp_path / "iam"),
    )

    from deadman.web.routes import admin as admin_routes
    from deadman.web.routes import iam as iam_routes
    from deadman.web.routes import resources as res_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(admin_routes.router)
    fresh.include_router(iam_routes.router)
    fresh.include_router(res_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestPromptVersions:
    def test_versions_and_rollback(self, client):
        client.post("/api/admin/prompts", json={"name": "pv", "content": "v1"})
        client.put("/api/admin/prompts/pv", json={"content": "v2"})
        v = client.get("/api/admin/prompts/pv/versions").json()
        assert len(v["versions"]) == 1 and v["current_version"] == 2
        rb = client.post("/api/admin/prompts/pv/rollback", json={"version": 1}).json()
        assert rb["ok"] is True
        assert client.get("/api/admin/prompts/pv").json()["content"] == "v1"


class TestIam:
    def test_keys(self, client):
        k = client.post("/api/admin/iam/keys", json={"label": "dev"}).json()
        assert k.get("api_key")
        assert client.delete(f"/api/admin/iam/keys/{k['key_id']}").json()["revoked"] is True

    def test_permissions(self, client):
        d = client.get("/api/admin/iam/permissions").json()
        assert len(d["matrix"]) >= 5

    def test_update_user_validation(self, client):
        r = client.patch("/api/admin/iam/users/nonexistent", json={"role": "admin"})
        assert r.status_code in (400, 404)


class TestTraces:
    def test_traces(self, client):
        r = client.get("/api/admin/traces?limit=5")
        assert r.status_code == 200 and "spans" in r.json()
