"""测试 deadman.web.server Phase 16C 端点

覆盖点（>= 12 个）：
  - GET /privacy /terms /support 返回 HTML
  - GET /api/support/tickets 走 auth（无 token 返回 401）
  - POST /api/support/tickets 创建工单（需 token）
  - GET /api/onboarding 未认证返回 401 / 已认证返回 null profile
  - POST /api/onboarding 保存画像
  - GET /api/onboarding/step/<idx> 返回步骤
  - 越权访问返回 404
  - 工单详情端点

测试方式：启动真实 HTTP server 在随机端口，发真实 HTTP 请求（参考 test_web_chat_graph.py）。
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore
from deadman.web.server import WebServer

# =====================================================================
# 辅助：启动真实 HTTP server
# =====================================================================


def _get_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(port: int, timeout: float = 8.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            conn.close()
            if resp.status == 200:
                return True
        except (ConnectionError, OSError):
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def http_server(tmp_path: Path, monkeypatch) -> int:
    """启动真实 HTTP server，返回 port

    monkeypatch：
    - settings.auth_data_dir → tmp_path/auth
    - settings.jwt_secret → ""
    - support.store._DEFAULT_DATA_DIR → tmp_path/support
    - onboarding.store._DEFAULT_DATA_DIR → tmp_path/onboarding
    """
    from deadman.config import settings
    monkeypatch.setattr(settings, "auth_data_dir", tmp_path / "auth")
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)

    # 重定向 TicketStore / OnboardingStore 默认路径
    import deadman.onboarding.store as obs
    import deadman.support.store as ssm
    monkeypatch.setattr(ssm, "_DEFAULT_DATA_DIR", tmp_path / "support")
    monkeypatch.setattr(obs, "_DEFAULT_DATA_DIR", tmp_path / "onboarding")

    port = _get_free_port()
    server = WebServer()
    thread = threading.Thread(
        target=server.run,
        args=("127.0.0.1", port),
        daemon=True,
    )
    thread.start()
    assert _wait_for_server(port), "服务器未在超时内启动"
    yield port
    # daemon 线程会随进程退出


def _register_user(tmp_path: Path, email: str = "alice@example.com", password: str = "password123") -> dict:
    """直接用 UserStore 注册一个用户

    注意：UserStore 写入 tmp_path/auth/{users.json, jwt_secret}。
    其中 jwt_secret 用于 HMAC 邮箱索引（_email_hmac），不是 JWT 签名密钥。
    JWT 签名密钥由 JWTManager._load_or_create_secret 读取 ~/.deadman/auth/jwt_secret。
    """
    store = UserStore(data_dir=tmp_path / "auth")
    return store.register(email, password, email.split("@")[0].capitalize())


def _issue_token(tmp_path: Path, user: dict) -> str:
    """签发 token - 必须与 server._get_jwt_manager 使用同一份 JWT secret

    server._get_jwt_manager 的逻辑：
        secret = settings.jwt_secret or None
        JWTManager(secret=secret, expiry_days=settings.jwt_expiry_days)
    当 settings.jwt_secret 为空时，JWTManager 从 ~/.deadman/auth/jwt_secret 读取默认 secret。
    本 helper 必须镜像该逻辑，否则 token 验证会失败（401）。
    """
    from deadman.config import settings
    secret = settings.jwt_secret or None
    mgr = JWTManager(secret=secret, expiry_days=settings.jwt_expiry_days)
    return mgr.issue(user)


def _request(port: int, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict | str, dict]:
    """发 HTTP 请求，返回 (status, body_dict_or_text, headers)"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    else:
        data = b""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    status = resp.status
    # 尝试 JSON 解析，失败返回文本
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    # 收集 headers（小写键）
    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return status, parsed, resp_headers


# =====================================================================
# 1. GET /privacy /terms /support 返回 HTML
# =====================================================================


class TestDocsPages:
    """测试 GET /privacy /terms /support 返回 HTML"""

    def test_get_privacy_returns_html(self, http_server):
        port = http_server
        status, body, headers = _request(port, "GET", "/privacy")
        assert status == 200
        assert "text/html" in headers.get("content-type", "")
        assert isinstance(body, str)
        assert "隐私政策" in body
        assert "PIPL" in body

    def test_get_terms_returns_html(self, http_server):
        port = http_server
        status, body, headers = _request(port, "GET", "/terms")
        assert status == 200
        assert "text/html" in headers.get("content-type", "")
        assert isinstance(body, str)
        assert "用户协议" in body
        assert "终活笔记不是法律文件" in body

    def test_get_support_returns_html(self, http_server):
        port = http_server
        status, body, headers = _request(port, "GET", "/support")
        assert status == 200
        assert "text/html" in headers.get("content-type", "")
        assert isinstance(body, str)
        assert "常见问题" in body
        assert "12345" in body  # 热线

    def test_get_nonexistent_doc_returns_404(self, http_server):
        # 没有 /nonexistent 路由 → 走静态文件 fallback → 404
        port = http_server
        status, _, _ = _request(port, "GET", "/nonexistent-doc-xyz")
        assert status == 404


# =====================================================================
# 2. /api/support/tickets 走 auth
# =====================================================================


class TestSupportTicketsAuth:
    """测试 /api/support/tickets 需要认证"""

    def test_get_tickets_without_token_returns_401(self, http_server, tmp_path):
        port = http_server
        status, body, _ = _request(port, "GET", "/api/support/tickets")
        assert status == 401
        assert "error" in body
        assert "未认证" in body["error"]

    def test_get_tickets_with_token_returns_empty_list(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "GET", "/api/support/tickets", token=token)
        assert status == 200
        assert body["count"] == 0
        assert body["tickets"] == []
        assert "disclaimer" in body

    def test_get_tickets_with_invalid_token_returns_401(self, http_server):
        port = http_server
        status, _body, _ = _request(port, "GET", "/api/support/tickets", token="invalid.token")
        assert status == 401


# =====================================================================
# 3. POST /api/support/tickets 创建工单
# =====================================================================


class TestSupportTicketCreate:
    """测试 POST /api/support/tickets 创建工单"""

    def test_create_ticket_without_token_returns_401(self, http_server):
        port = http_server
        status, _body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "测试工单",
            "description": "测试描述",
        })
        assert status == 401

    def test_create_ticket_with_token_returns_201(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "如何办理户口注销？",
            "description": "需要哪些材料？",
        }, token=token)
        assert status == 201
        assert "ticket" in body
        t = body["ticket"]
        assert t["ticket_id"].startswith("tkt-")
        assert t["status"] == "open"
        assert t["subject"] == "如何办理户口注销？"
        assert t["user_id"] == user["user_id"]
        assert "disclaimer" in body

    def test_create_ticket_invalid_category_returns_400(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "无效类别",
            "priority": "普通",
            "subject": "x",
            "description": "y",
        }, token=token)
        assert status == 400
        assert "error" in body
        assert "category" in body["error"]

    def test_create_ticket_missing_subject_returns_400(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, _body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "",
            "description": "y",
        }, token=token)
        assert status == 400


# =====================================================================
# 4. GET /api/support/tickets/<id> 详情 + 越权
# =====================================================================


class TestSupportTicketGet:
    """测试 GET /api/support/tickets/<id>"""

    def test_get_ticket_detail_returns_200(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        # 先创建
        _, create_body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "测试",
            "description": "测试描述",
        }, token=token)
        ticket_id = create_body["ticket"]["ticket_id"]
        # 再查详情
        status, body, _ = _request(port, "GET", f"/api/support/tickets/{ticket_id}", token=token)
        assert status == 200
        assert body["ticket"]["ticket_id"] == ticket_id
        assert "disclaimer" in body

    def test_get_ticket_other_user_returns_404(self, http_server, tmp_path):
        """user2 不能读取 user1 的工单"""
        port = http_server
        user1 = _register_user(tmp_path, "alice@example.com")
        user2 = _register_user(tmp_path, "bob@example.com")
        token1 = _issue_token(tmp_path, user1)
        token2 = _issue_token(tmp_path, user2)
        # user1 创建
        _, create_body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "私密",
            "description": "私密内容",
        }, token=token1)
        ticket_id = create_body["ticket"]["ticket_id"]
        # user2 试图读取
        status, body, _ = _request(port, "GET", f"/api/support/tickets/{ticket_id}", token=token2)
        assert status == 404
        assert "无权限" in body["error"]

    def test_get_ticket_without_token_returns_401(self, http_server, tmp_path):
        port = http_server
        status, _body, _ = _request(port, "GET", "/api/support/tickets/tkt-nonexistent")
        assert status == 401


# =====================================================================
# 5. POST /api/support/tickets/<id>/replies
# =====================================================================


class TestSupportTicketReply:
    def test_reply_appended_returns_200(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        # 创建工单
        _, create_body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "测试",
            "description": "测试描述",
        }, token=token)
        ticket_id = create_body["ticket"]["ticket_id"]
        # 追加回复
        status, body, _ = _request(port, "POST", f"/api/support/tickets/{ticket_id}/replies", body={
            "content": "补充：逝者在北京",
        }, token=token)
        assert status == 200
        assert body["reply"]["content"] == "补充：逝者在北京"
        assert body["reply"]["author"] == "user"

    def test_reply_to_other_user_ticket_returns_404(self, http_server, tmp_path):
        port = http_server
        user1 = _register_user(tmp_path, "alice@example.com")
        user2 = _register_user(tmp_path, "bob@example.com")
        token1 = _issue_token(tmp_path, user1)
        token2 = _issue_token(tmp_path, user2)
        _, create_body, _ = _request(port, "POST", "/api/support/tickets", body={
            "category": "咨询",
            "priority": "普通",
            "subject": "x",
            "description": "y",
        }, token=token1)
        ticket_id = create_body["ticket"]["ticket_id"]
        status, _body, _ = _request(port, "POST", f"/api/support/tickets/{ticket_id}/replies", body={
            "content": "恶意回复",
        }, token=token2)
        assert status == 404


# =====================================================================
# 6. GET /api/onboarding
# =====================================================================


class TestOnboardingGet:
    def test_get_onboarding_without_token_returns_401(self, http_server):
        port = http_server
        status, _body, _ = _request(port, "GET", "/api/onboarding")
        assert status == 401

    def test_get_onboarding_empty_returns_null(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "GET", "/api/onboarding", token=token)
        assert status == 200
        assert body["completed"] is False
        assert body["profile"] is None
        assert "disclaimer" in body


# =====================================================================
# 7. POST /api/onboarding 保存
# =====================================================================


class TestOnboardingSave:
    def test_save_onboarding_returns_200(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "POST", "/api/onboarding", body={
            "relationship": "亲属",
            "location": "北京",
            "death_date": "2024-01-15",
            "current_stage": ["死亡证明"],
            "consent": True,
        }, token=token)
        assert status == 200
        assert body["completed"] is True
        assert body["profile"]["relationship"] == "亲属"
        assert body["profile"]["location"] == "北京"
        assert body["user_profile"]["relationship"] == "亲属"
        assert body["user_profile"]["source"] == "onboarding_wizard"

    def test_save_onboarding_invalid_category_returns_400(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "POST", "/api/onboarding", body={
            "relationship": "陌生人",  # 非法
            "location": "北京",
            "death_date": "",
            "current_stage": [],
            "consent": True,
        }, token=token)
        assert status == 400
        assert "error" in body

    def test_save_onboarding_consent_false_returns_400(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        status, body, _ = _request(port, "POST", "/api/onboarding", body={
            "relationship": "亲属",
            "location": "北京",
            "death_date": "",
            "current_stage": [],
            "consent": False,  # 未同意
        }, token=token)
        assert status == 400
        assert "consent" in body["error"]

    def test_save_onboarding_without_token_returns_401(self, http_server):
        port = http_server
        status, _body, _ = _request(port, "POST", "/api/onboarding", body={
            "relationship": "亲属",
            "location": "北京",
            "death_date": "",
            "current_stage": [],
            "consent": True,
        })
        assert status == 401

    def test_save_then_get_returns_saved_profile(self, http_server, tmp_path):
        port = http_server
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        # 保存
        _request(port, "POST", "/api/onboarding", body={
            "relationship": "朋友",
            "location": "上海",
            "death_date": "2024-06-01",
            "current_stage": [],
            "consent": True,
        }, token=token)
        # 再 GET
        status, body, _ = _request(port, "GET", "/api/onboarding", token=token)
        assert status == 200
        assert body["completed"] is True
        assert body["profile"]["relationship"] == "朋友"
        assert body["profile"]["location"] == "上海"


# =====================================================================
# 8. GET /api/onboarding/step/<idx>
# =====================================================================


class TestOnboardingStep:
    def test_get_step_0_returns_relationship(self, http_server):
        port = http_server
        status, body, _ = _request(port, "GET", "/api/onboarding/step/0")
        assert status == 200
        assert body["step"]["key"] == "relationship"
        assert body["total_steps"] == 5

    def test_get_step_4_returns_consent(self, http_server):
        port = http_server
        status, body, _ = _request(port, "GET", "/api/onboarding/step/4")
        assert status == 200
        assert body["step"]["key"] == "consent"

    def test_get_step_out_of_range_returns_400(self, http_server):
        port = http_server
        status, _body, _ = _request(port, "GET", "/api/onboarding/step/99")
        assert status == 400

    def test_get_step_non_integer_returns_400(self, http_server):
        port = http_server
        status, _body, _ = _request(port, "GET", "/api/onboarding/step/abc")
        assert status == 400


# =====================================================================
# 9. 整合：未认证统一返回 401
# =====================================================================


class TestUnauthorizedAccess:
    """所有 /api/support/* 与 /api/onboarding POST 端点未认证返回 401"""

    def test_post_support_tickets_unauthorized(self, http_server):
        port = http_server
        status, _, _ = _request(port, "POST", "/api/support/tickets", body={"category": "咨询", "priority": "普通", "subject": "x", "description": "y"})
        assert status == 401

    def test_post_onboarding_unauthorized(self, http_server):
        port = http_server
        status, _, _ = _request(port, "POST", "/api/onboarding", body={"relationship": "亲属", "location": "北京", "consent": True})
        assert status == 401

    def test_post_reply_unauthorized(self, http_server):
        port = http_server
        status, _, _ = _request(port, "POST", "/api/support/tickets/tkt-nonexistent/replies", body={"content": "x"})
        assert status == 401
