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


class TestExport:
    def test_export_md(self, client):
        r = client.post(
            "/api/chat/export", json={"text": "# 标题\n\n正文", "format": "md", "filename": "x"}
        )
        assert r.status_code == 200 and len(r.content) > 0

    def test_export_docx(self, client):
        r = client.post(
            "/api/chat/export",
            json={"text": "# 清单\n\n- 项目1", "format": "docx", "filename": "x"},
        )
        assert r.status_code == 200 and len(r.content) > 1000

    def test_export_pdf(self, client):
        r = client.post(
            "/api/chat/export",
            json={"text": "# 报告\n\n正文内容", "format": "pdf", "filename": "x"},
        )
        assert r.status_code == 200 and r.content[:4] == b"%PDF"

    def test_export_bad_format(self, client):
        r = client.post("/api/chat/export", json={"text": "x", "format": "exe", "filename": "x"})
        assert r.status_code in (400, 422)


class TestPlot:
    def test_plot_generates_image(self, client):
        # Docker 沙箱默认镜像为 python:3.12-slim（无 matplotlib），绘图能力依赖执行环境；
        # 本地/非 Docker 后端（安装有 matplotlib）才可出图，否则跳过该断言路径。
        try:
            from deadman.sandbox import SandboxManager

            if SandboxManager().get_active_backend() == "docker":
                pytest.skip("Docker 沙箱镜像无 matplotlib，跳过绘图断言")
        except Exception:
            pass
        r = client.post(
            "/api/chat/plot",
            json={
                "code": "import matplotlib\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3],[3,1,2])\nplt.show()"
            },
        )
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True and d.get("image_base64")

    def test_plot_no_image(self, client):
        r = client.post("/api/chat/plot", json={"code": "print('hello')"})
        d = r.json()
        assert d.get("ok") is False


class TestHotlineInstitution:
    def test_hotline_command(self, client):
        r = client.post("/api/chat/command", json={"command": "/hotline"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert "热线" in r.json()["text"]

    def test_institution_command(self, client):
        r = client.post("/api/chat/command", json={"command": "/institution"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert "机构" in r.json()["text"]

    def test_unknown_still_works(self, client):
        r = client.post("/api/chat/command", json={"command": "/nope"})
        assert r.json()["ok"] is False


class TestSkillState:
    def test_skill_toggle(self, client):
        r = client.post("/api/chat/command", json={"command": "/skill disable demo"})
        d = r.json()
        assert d["ok"] is True and "已停用" in d["text"]
        r2 = client.post("/api/chat/command", json={"command": "/skill enable demo"})
        assert "已启用" in r2.json()["text"]


class TestFullCommands:
    def test_task_add_and_list(self, client):
        r = client.post("/api/chat/command", json={"command": "/task add 0 9 * * * 提醒办理"})
        assert r.json()["ok"] is True and "已提议" in r.json()["text"]
        assert (
            "定时任务"
            in client.post("/api/chat/command", json={"command": "/task list"}).json()["text"]
        )

    def test_vault_note_docs_switch(self, client):
        for cmd in ["/vault list", "/note list", "/docs list", "/switch status"]:
            d = client.post("/api/chat/command", json={"command": cmd}).json()
            assert "text" in d
