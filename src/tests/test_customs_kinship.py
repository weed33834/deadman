"""民俗规则 + 亲属图谱测试（独立 FastAPI 实例隔离）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.customs as cu
    import deadman.web.routes.kinship as ki

    monkeypatch.setattr(cu, "_CUSTOMS_DIR", tmp_path / "customs")
    monkeypatch.setattr(ki, "_DIR", tmp_path / "kinship")

    from deadman.web.routes import customs as cu_routes
    from deadman.web.routes import kinship as ki_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(cu_routes.router)
    fresh.include_router(ki_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestCustoms:
    def test_presets_and_import(self, client):
        assert len(client.get("/api/customs/presets").json()["presets"]) >= 4
        r = client.post("/api/customs/import/funeral-cn-common")
        assert r.status_code == 200 and r.json()["custom"]["region"] == "中国·通用"
        # 预置含 7 个烧七
        assert len(r.json()["custom"]["weekly_observances"]) == 7

    def test_custom_crud(self, client):
        r = client.post(
            "/api/customs",
            json={
                "region": "我家",
                "title": "自家规矩",
                "rules": [{"title": "头七", "detail": "吃素"}],
            },
        )
        assert r.status_code == 200
        cid = client.get("/api/customs").json()["customs"][0]["id"]
        assert client.get("/api/customs?q=头七").json()["count"] >= 1
        assert client.delete(f"/api/customs/{cid}").json()["deleted"] is True


class TestKinship:
    def test_member_and_graph(self, client):
        assert (
            client.post("/api/kinship/member", json={"name": "父亲", "gender": "male"}).json()[
                "member"
            ]["name"]
            == "父亲"
        )
        assert (
            client.post("/api/kinship/member", json={"name": "母亲", "gender": "female"}).json()[
                "ok"
            ]
            is True
        )
        g = client.get("/api/kinship").json()["graph"]
        assert len(g["nodes"]) == 2
        # 加关系
        data = client.get("/api/kinship").json()
        m1, m2 = data["members"][0], data["members"][1]
        r = client.post(
            "/api/kinship/relation", json={"from": m1["id"], "to": m2["id"], "type": "spouse"}
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        g2 = client.get("/api/kinship").json()["graph"]
        assert len(g2["edges"]) == 1

    def test_invalid_relation(self, client):
        client.post("/api/kinship/member", json={"name": "A"})
        r = client.post("/api/kinship/relation", json={"from": "m-x", "to": "m-y", "type": "bad"})
        assert r.status_code in (400, 422)
