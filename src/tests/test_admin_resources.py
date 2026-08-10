"""G1 资源服务测试：管理台真实增删改调 + 测试台 + 备份（独立 FastAPI 实例隔离）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.resources as res

    monkeypatch.setattr(res, "_ADMIN_DIR", tmp_path)
    for name in ("_prompts_store", "_agents_store", "_voices_store", "_tool_runs_store"):
        monkeypatch.setattr(res, name, res._JsonStore(name.replace("_store", "") + ".json"))

    from deadman.web.routes import admin as admin_routes
    from deadman.web.routes import mcp as mcp_routes
    from deadman.web.routes import voice as voice_routes

    fresh = FastAPI()
    fresh.include_router(admin_routes.router)
    fresh.include_router(mcp_routes.router)
    fresh.include_router(res.router)
    fresh.include_router(voice_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestPrompts:
    def test_crud_roundtrip(self, client):
        r = client.post(
            "/api/admin/prompts", json={"name": "p1", "content": "系统提示", "description": "d"}
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        names = [p["name"] for p in client.get("/api/admin/prompts").json()["prompts"]]
        assert "p1" in names
        assert client.get("/api/admin/prompts/p1").json()["content"] == "系统提示"
        r = client.put("/api/admin/prompts/p1", json={"content": "v2"})
        assert r.json()["version"] == 2
        assert client.delete("/api/admin/prompts/p1").json()["deleted"] is True
        assert client.get("/api/admin/prompts/p1").status_code == 404

    def test_prompt_test_returns_structured(self, client):
        client.post("/api/admin/prompts", json={"name": "pt", "content": "你是助手"})
        r = client.post("/api/admin/prompts/pt/test", json={"input_text": "hi"})
        assert r.status_code == 200 and "ok" in r.json()


class TestAgents:
    def test_create_list_delete(self, client):
        assert client.post(
            "/api/admin/agents", json={"id": "a1", "name": "A1", "system_prompt": "sp"}
        ).json()["ok"]
        ids = [a["id"] for a in client.get("/api/admin/agents").json()["agents"]]
        assert "a1" in ids and "death-aftercare" in ids
        assert client.delete("/api/admin/agents/a1").json()["deleted"] is True
        assert client.delete("/api/admin/agents/death-aftercare").status_code == 400


class TestVoices:
    def test_voices_crud_and_default(self, client):
        assert len(client.get("/api/admin/voices").json()["voices"]) >= 4
        assert client.post("/api/admin/voices", json={"id": "v1", "name": "新音色"}).json()["ok"]
        assert len(client.get("/api/admin/voices").json()["voices"]) == 5
        assert client.post("/api/admin/voices/v1/set-default").json()["ok"]
        assert client.delete("/api/admin/voices/v1").json()["deleted"] is True
        assert client.delete("/api/admin/voices/gentle_male").status_code == 400


class TestTools:
    def test_tool_test_runs_and_logs(self, client):
        r = client.post(
            "/api/admin/tools/test",
            json={
                "name": "query_knowledge",
                "arguments": {"country": "CN", "topic": "death_certificate"},
            },
        )
        assert r.status_code == 200 and "duration_ms" in r.json()
        assert len(client.get("/api/admin/tools/runs").json()["runs"]) >= 1


class TestBackup:
    def test_export_import(self, client):
        client.post("/api/admin/prompts", json={"name": "b1", "content": "x"})
        client.post("/api/admin/voices", json={"id": "bv", "name": "nv"})
        pkg = client.get("/api/admin/backup/export").json()["package"]
        assert "b1" in pkg["prompts"]
        r = client.post("/api/admin/backup/import", json=pkg)
        assert r.status_code == 200 and r.json()["ok"]
        assert any(x.startswith("prompts") for x in r.json()["imported"])
