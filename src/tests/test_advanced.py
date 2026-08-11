"""高级通用能力测试 —— 发布灰度/A-B/模板/AI用例/漂移/多租户/节点暂停（独立 FastAPI 隔离）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.advanced as adv
    import deadman.web.routes.resources as res

    monkeypatch.setattr(res, "_ADMIN_DIR", tmp_path)
    monkeypatch.setattr(adv, "_ADMIN_DIR", tmp_path)
    for name in ("_prompts_store", "_agents_store", "_voices_store", "_tool_runs_store"):
        monkeypatch.setattr(res, name, res._JsonStore(name.replace("_store", "") + ".json"))

    from deadman.web.routes import advanced as adv_routes
    from deadman.web.routes import resources as res_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(adv_routes.router)
    fresh.include_router(res_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestAgentPublish:
    def test_publish_and_rollout(self, client):
        client.post("/api/admin/agents", json={"id": "pub1", "name": "x", "system_prompt": "sp"})
        r = client.post("/api/admin/agents/pub1/publish")
        assert r.json()["status"] == "published" and r.json()["version"] == 2
        assert (
            client.post("/api/admin/agents/pub1/rollout", json={"ratio": 0.3}).json()[
                "rollout_ratio"
            ]
            == 0.3
        )
        assert len(client.get("/api/admin/agents/pub1/versions").json()["versions"]) >= 1


class TestPromptAB:
    def test_ab(self, client):
        client.post("/api/admin/prompts", json={"name": "ab1", "content": "A"})
        r = client.put(
            "/api/admin/prompts/ab1/ab",
            json={"enabled": True, "ratio": 0.5, "variant_b_content": "B"},
        )
        assert r.json()["ab"]["ratio"] == 0.5
        assert client.get("/api/admin/prompts/ab1/ab").json()["ab"]["enabled"] is True


class TestTemplates:
    def test_list_and_import(self, client):
        assert len(client.get("/api/admin/templates").json()["templates"]) >= 5
        r = client.post("/api/admin/templates/prompt_legal_advisor/import")
        assert r.json()["ok"] is True and r.json()["imported"] == "prompt"
        r2 = client.post("/api/admin/templates/agent_research/import")
        assert r2.json()["imported"] == "agent"


class TestDriftTenantNode:
    def test_drift(self, client):
        assert (
            client.post("/api/admin/drift/baseline", json={"sample": "x"}).json()["captured"]
            is True
        )
        assert client.get("/api/admin/drift").json()["status"] in ("stable", "no_baseline")

    def test_tenant(self, client):
        assert (
            client.post("/api/admin/tenants", json={"name": "家庭A"}).json()["tenant"]["name"]
            == "家庭A"
        )
        assert len(client.get("/api/admin/tenants").json()["tenants"]) == 1

    def test_node_pause_resume(self, client):
        assert (
            client.post("/api/admin/orchestration/policy-researcher/pause").json()["state"]
            == "paused"
        )
        assert (
            client.post("/api/admin/orchestration/policy-researcher/resume").json()["state"]
            == "running"
        )
