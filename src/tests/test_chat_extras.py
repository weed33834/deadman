"""对话增强测试 —— 文件解析上传 + 对话命令（/prompt /expert /skill）+ 知识库引用"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 隔离 admin 持久化目录到临时目录
    import deadman.web.routes.chat_extras as ce

    monkeypatch.setattr(ce, "_admin_dir", lambda: tmp_path)

    from deadman.web.routes import chat_extras as chat_routes

    fresh = FastAPI()
    from fastapi.responses import JSONResponse

    from deadman.errors import DeadmanError

    @fresh.exception_handler(DeadmanError)
    async def _deadman(request, exc: DeadmanError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict("-"))

    fresh.include_router(chat_routes.router)
    c = TestClient(fresh)
    yield c
    c.close()


class TestCommands:
    def test_prompt_list(self, client):
        r = client.post("/api/chat/command", json={"command": "/prompt list"})
        assert r.status_code == 200 and r.json()["kind"] == "list"

    def test_prompt_new_and_get(self, client):
        r = client.post("/api/chat/command", json={"command": "/prompt new mytest 你是测试助手"})
        assert r.json()["ok"] is True and "mytest" in r.json()["text"]
        r2 = client.post("/api/chat/command", json={"command": "/prompt get mytest"})
        assert r2.json()["ok"] is True and "你是测试助手" in r2.json()["text"]

    def test_expert_help_and_new(self, client):
        assert (
            client.post("/api/chat/command", json={"command": "/expert help"}).json()["ok"] is True
        )
        r = client.post(
            "/api/chat/command", json={"command": "/expert new doc_help 文档助手 你是文档处理专家"}
        )
        assert r.json()["ok"] is True and "doc_help" in r.json()["text"]

    def test_unknown_command(self, client):
        r = client.post("/api/chat/command", json={"command": "/nope x"})
        assert r.json()["ok"] is False and "未知命令" in r.json()["text"]

    def test_non_slash(self, client):
        r = client.post("/api/chat/command", json={"command": "hello"})
        assert r.json()["ok"] is False


class TestUpload:
    def test_upload_txt(self, client):
        r = client.post(
            "/api/chat/upload",
            files={"file": ("a.txt", "身后事办理流程测试内容".encode(), "text/plain")},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["char_count"] == 11

    def test_upload_empty(self, client):
        r = client.post("/api/chat/upload", files={"file": ("b.txt", b"", "text/plain")})
        assert r.status_code in (400, 200)
        d = r.json()
        assert d.get("ok") is False or d.get("char_count", 0) == 0


class TestKb:
    def test_kb_query(self, client):
        r = client.post("/api/chat/kb", json={"query": "死亡证明", "country": "CN"})
        assert r.status_code == 200
        assert "ok" in r.json() or "result" in r.json()
