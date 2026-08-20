"""G6 画布 + G10 浏览器自动化测试（独立 FastAPI 实例隔离）"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import deadman.web.routes.canvas as cv

    monkeypatch.setattr(
        cv,
        "_canvas_dir",
        lambda: (lambda d: (d.mkdir(parents=True, exist_ok=True), d)[1])(tmp_path / "canvas"),
    )
    from deadman.web.routes import canvas as canvas_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(canvas_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestCanvas:
    def test_crud(self, client):
        cid = client.post("/api/canvas", json={"title": "悼文"}).json()["canvas"]["id"]
        client.put(
            f"/api/canvas/{cid}", json={"blocks": [{"type": "text", "content": "亲爱的父亲…"}]}
        )
        assert len(client.get(f"/api/canvas/{cid}").json()["canvas"]["blocks"]) == 1
        assert len(client.get("/api/canvas").json()["canvases"]) == 1
        assert client.delete(f"/api/canvas/{cid}").json()["deleted"] is True

    def test_ai_invalid_index(self, client):
        cid = client.post("/api/canvas", json={"title": "x"}).json()["canvas"]["id"]
        r = client.post(f"/api/canvas/{cid}/ai", json={"block_index": 5, "instruction": "续写"})
        assert r.status_code in (400, 422)

    def test_delete_missing(self, client):
        assert client.delete("/api/canvas/nope").status_code in (400, 404)
