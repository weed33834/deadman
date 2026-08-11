"""会话管理测试 —— 多会话 / 历史（独立 FastAPI 实例隔离）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.sessions as ss

    monkeypatch.setattr(ss, "_sessions_dir", lambda: tmp_path)
    from deadman.web.routes import sessions as sessions_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(sessions_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestSessions:
    def test_crud_roundtrip(self, client):
        r = client.post("/api/sessions", json={"title": "会话A"})
        assert r.status_code == 200 and r.json()["ok"] is True
        sid = r.json()["session"]["id"]

        client.post(
            f"/api/sessions/{sid}/messages", json={"role": "user", "content": "身后事怎么办"}
        )
        client.post(
            f"/api/sessions/{sid}/messages", json={"role": "assistant", "content": "先办死亡证明"}
        )

        msgs = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
        assert len(msgs) == 2 and msgs[0]["role"] == "user"

        sessions = client.get("/api/sessions").json()["sessions"]
        assert len(sessions) == 1 and sessions[0]["message_count"] == 2

        assert client.delete(f"/api/sessions/{sid}").json()["deleted"] is True
        assert client.get(f"/api/sessions/{sid}/messages").status_code in (400, 404)

    def test_title_from_first_user_message(self, client):
        sid = client.post("/api/sessions", json={"title": ""}).json()["session"]["id"]
        client.post(
            f"/api/sessions/{sid}/messages", json={"role": "user", "content": "死亡证明如何办理"}
        )
        r = client.get("/api/sessions").json()["sessions"][0]
        assert r["title"] == "死亡证明如何办理"

    def test_invalid_role(self, client):
        sid = client.post("/api/sessions", json={"title": "x"}).json()["session"]["id"]
        r = client.post(f"/api/sessions/{sid}/messages", json={"role": "robot", "content": "hi"})
        assert r.status_code in (400, 422)


class TestSearchGroup:
    def test_patch_group_and_search(self, client):
        sid = client.post("/api/sessions", json={"title": "身后事"}).json()["session"]["id"]
        client.post(
            f"/api/sessions/{sid}/messages", json={"role": "user", "content": "死亡证明怎么办"}
        )
        client.patch(f"/api/sessions/{sid}", json={"group": "家庭"})
        # list 含 group
        s = client.get("/api/sessions").json()["sessions"][0]
        assert s.get("group") == "家庭"
        # 搜索
        r = client.get("/api/sessions/search?q=死亡").json()
        assert len(r["sessions"]) == 1
        # 分组
        g = client.get("/api/sessions/groups").json()["groups"]
        assert any(x["name"] == "家庭" for x in g)
