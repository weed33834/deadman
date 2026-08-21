"""测试 deadman.web.app Phase 16C 端点（FastAPI TestClient 进程内）

覆盖点（>= 12 个）：
  - GET /privacy /terms /support 返回 HTML
  - GET /api/support/tickets 走 auth（无 token 返回 401）
  - POST /api/support/tickets 创建工单（需 token）
  - GET /api/onboarding 未认证返回 401 / 已认证返回 null profile
  - POST /api/onboarding 保存画像
  - GET /api/onboarding/step/<idx> 返回步骤
  - 越权访问返回 404
  - 工单详情端点

测试方式：TestClient 进程内调真实端点，不启 HTTP server。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore

# =====================================================================
# 辅助：TestClient fixture + 请求 helper
# =====================================================================


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """TestClient，数据目录全部指向 tmp_path"""
    from fastapi.testclient import TestClient

    from deadman.config import settings
    from deadman.web.app import app

    monkeypatch.setattr(settings, "auth_data_dir", tmp_path / "auth")
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)

    # 重定向 TicketStore / OnboardingStore 默认路径
    import deadman.onboarding.store as obs
    import deadman.support.store as ssm

    monkeypatch.setattr(ssm, "_DEFAULT_DATA_DIR", tmp_path / "support")
    monkeypatch.setattr(obs, "_DEFAULT_DATA_DIR", tmp_path / "onboarding")

    return TestClient(app)


def _register_user(
    tmp_path: Path, email: str = "alice@example.com", password: str = "password123"
) -> dict:
    """直接用 UserStore 注册一个用户

    注意：UserStore 写入 tmp_path/auth/{users.json, jwt_secret}。
    其中 jwt_secret 用于 HMAC 邮箱索引（_email_hmac），不是 JWT 签名密钥。
    JWT 签名密钥由 JWTManager._load_or_create_secret 读取 ~/.deadman/auth/jwt_secret。
    """
    store = UserStore(data_dir=tmp_path / "auth")
    return store.register(email, password, email.split("@")[0].capitalize())


def _issue_token(tmp_path: Path, user: dict) -> str:
    """签发 token - 必须与 deps.get_jwt_manager 使用同一份 JWT secret"""
    from deadman.config import settings

    secret = settings.jwt_secret or None
    mgr = JWTManager(secret=secret, expiry_days=settings.jwt_expiry_days)
    return mgr.issue(user)


# =====================================================================
# 1. GET /privacy /terms /support 返回 HTML
# =====================================================================


class TestDocsPages:
    """测试 GET /privacy /terms /support 返回 HTML"""

    def test_get_privacy_returns_html(self, client):
        r = client.get("/privacy")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "隐私政策" in r.text
        assert "PIPL" in r.text

    def test_get_terms_returns_html(self, client):
        r = client.get("/terms")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "用户协议" in r.text
        assert "终活笔记不是法律文件" in r.text

    def test_get_support_returns_html(self, client):
        r = client.get("/support")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "常见问题" in r.text
        assert "12345" in r.text  # 热线

    def test_get_nonexistent_doc_returns_404(self, client):
        # 没有 /nonexistent 路由 → 走静态文件 fallback → 404
        r = client.get("/nonexistent-doc-xyz")
        assert r.status_code == 404


# =====================================================================
# 2. /api/support/tickets 走 auth
# =====================================================================


class TestSupportTicketsAuth:
    """测试 /api/support/tickets 需要认证"""

    def test_get_tickets_without_token_returns_401(self, client):
        r = client.get("/api/support/tickets")
        assert r.status_code == 401

    def test_get_tickets_with_token_returns_empty_list(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.get("/api/support/tickets", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["tickets"] == []
        assert "disclaimer" in body

    def test_get_tickets_with_invalid_token_returns_401(self, client):
        r = client.get("/api/support/tickets", headers={"Authorization": "Bearer invalid.token"})
        assert r.status_code == 401


# =====================================================================
# 3. POST /api/support/tickets 创建工单
# =====================================================================


class TestSupportTicketCreate:
    """测试 POST /api/support/tickets 创建工单"""

    def test_create_ticket_without_token_returns_401(self, client):
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "测试工单",
                "description": "测试描述",
            },
        )
        assert r.status_code == 401

    def test_create_ticket_with_token_returns_201(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "如何办理户口注销？",
                "description": "需要哪些材料？",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        body = r.json()
        assert "ticket" in body
        t = body["ticket"]
        assert t["ticket_id"].startswith("tkt-")
        assert t["status"] == "open"
        assert t["subject"] == "如何办理户口注销？"
        assert t["user_id"] == user["user_id"]
        assert "disclaimer" in body

    def test_create_ticket_invalid_category_returns_400(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "无效类别",
                "priority": "普通",
                "subject": "x",
                "description": "y",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
        # FastAPI HTTPException 把业务 payload 包在 detail 里
        assert "category" in r.json()["detail"]["error"]

    def test_create_ticket_missing_subject_returns_400(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "",
                "description": "y",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400


# =====================================================================
# 4. GET /api/support/tickets/<id> 详情 + 越权
# =====================================================================


class TestSupportTicketGet:
    """测试 GET /api/support/tickets/<id>"""

    def test_get_ticket_detail_returns_200(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        headers = {"Authorization": f"Bearer {token}"}
        # 先创建
        create_body = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "测试",
                "description": "测试描述",
            },
            headers=headers,
        ).json()
        ticket_id = create_body["ticket"]["ticket_id"]
        # 再查详情
        r = client.get(f"/api/support/tickets/{ticket_id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["ticket"]["ticket_id"] == ticket_id
        assert "disclaimer" in r.json()

    def test_get_ticket_other_user_returns_404(self, client, tmp_path):
        """user2 不能读取 user1 的工单"""
        user1 = _register_user(tmp_path, "alice@example.com")
        user2 = _register_user(tmp_path, "bob@example.com")
        token1 = _issue_token(tmp_path, user1)
        token2 = _issue_token(tmp_path, user2)
        # user1 创建
        create_body = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "私密",
                "description": "私密内容",
            },
            headers={"Authorization": f"Bearer {token1}"},
        ).json()
        ticket_id = create_body["ticket"]["ticket_id"]
        # user2 试图读取
        r = client.get(
            f"/api/support/tickets/{ticket_id}", headers={"Authorization": f"Bearer {token2}"}
        )
        assert r.status_code == 404
        assert "无权限" in r.json()["detail"]["error"]

    def test_get_ticket_without_token_returns_401(self, client):
        r = client.get("/api/support/tickets/tkt-nonexistent")
        assert r.status_code == 401


# =====================================================================
# 5. POST /api/support/tickets/<id>/replies
# =====================================================================


class TestSupportTicketReply:
    def test_reply_appended_returns_200(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        headers = {"Authorization": f"Bearer {token}"}
        # 创建工单
        create_body = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "测试",
                "description": "测试描述",
            },
            headers=headers,
        ).json()
        ticket_id = create_body["ticket"]["ticket_id"]
        # 追加回复
        r = client.post(
            f"/api/support/tickets/{ticket_id}/replies",
            json={"content": "补充：逝者在北京"},
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["reply"]["content"] == "补充：逝者在北京"
        assert body["reply"]["author"] == "user"

    def test_reply_to_other_user_ticket_returns_404(self, client, tmp_path):
        user1 = _register_user(tmp_path, "alice@example.com")
        user2 = _register_user(tmp_path, "bob@example.com")
        token1 = _issue_token(tmp_path, user1)
        token2 = _issue_token(tmp_path, user2)
        create_body = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "x",
                "description": "y",
            },
            headers={"Authorization": f"Bearer {token1}"},
        ).json()
        ticket_id = create_body["ticket"]["ticket_id"]
        r = client.post(
            f"/api/support/tickets/{ticket_id}/replies",
            json={"content": "恶意回复"},
            headers={"Authorization": f"Bearer {token2}"},
        )
        assert r.status_code == 404


# =====================================================================
# 6. GET /api/onboarding
# =====================================================================


class TestOnboardingGet:
    def test_get_onboarding_without_token_returns_401(self, client):
        r = client.get("/api/onboarding")
        assert r.status_code == 401

    def test_get_onboarding_empty_returns_null(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.get("/api/onboarding", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["completed"] is False
        assert body["profile"] is None
        assert "disclaimer" in body


# =====================================================================
# 7. POST /api/onboarding 保存
# =====================================================================


class TestOnboardingSave:
    def test_save_onboarding_returns_200(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.post(
            "/api/onboarding",
            json={
                "relationship": "亲属",
                "location": "北京",
                "death_date": "2024-01-15",
                "current_stage": ["死亡证明"],
                "consent": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["completed"] is True
        assert body["profile"]["relationship"] == "亲属"
        assert body["profile"]["location"] == "北京"
        assert body["user_profile"]["relationship"] == "亲属"
        assert body["user_profile"]["source"] == "onboarding_wizard"

    def test_save_onboarding_invalid_category_returns_400(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.post(
            "/api/onboarding",
            json={
                "relationship": "陌生人",  # 非法
                "location": "北京",
                "death_date": "",
                "current_stage": [],
                "consent": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
        assert "relationship" in r.json()["detail"]["error"]

    def test_save_onboarding_consent_false_returns_400(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        r = client.post(
            "/api/onboarding",
            json={
                "relationship": "亲属",
                "location": "北京",
                "death_date": "",
                "current_stage": [],
                "consent": False,  # 未同意
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
        assert "consent" in r.json()["detail"]["error"]

    def test_save_onboarding_without_token_returns_401(self, client):
        r = client.post(
            "/api/onboarding",
            json={
                "relationship": "亲属",
                "location": "北京",
                "death_date": "",
                "current_stage": [],
                "consent": True,
            },
        )
        assert r.status_code == 401

    def test_save_then_get_returns_saved_profile(self, client, tmp_path):
        user = _register_user(tmp_path)
        token = _issue_token(tmp_path, user)
        headers = {"Authorization": f"Bearer {token}"}
        # 保存
        client.post(
            "/api/onboarding",
            json={
                "relationship": "朋友",
                "location": "上海",
                "death_date": "2024-06-01",
                "current_stage": [],
                "consent": True,
            },
            headers=headers,
        )
        # 再 GET
        r = client.get("/api/onboarding", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["completed"] is True
        assert body["profile"]["relationship"] == "朋友"
        assert body["profile"]["location"] == "上海"


# =====================================================================
# 8. GET /api/onboarding/step/<idx>
# =====================================================================


class TestOnboardingStep:
    def test_get_step_0_returns_relationship(self, client):
        r = client.get("/api/onboarding/step/0")
        assert r.status_code == 200
        assert r.json()["step"]["key"] == "relationship"
        assert r.json()["total_steps"] == 5

    def test_get_step_4_returns_consent(self, client):
        r = client.get("/api/onboarding/step/4")
        assert r.status_code == 200
        assert r.json()["step"]["key"] == "consent"

    def test_get_step_out_of_range_returns_400(self, client):
        r = client.get("/api/onboarding/step/99")
        assert r.status_code == 400

    def test_get_step_non_integer_returns_422(self, client):
        # FastAPI 路径参数类型校验：非整数返回 422
        r = client.get("/api/onboarding/step/abc")
        assert r.status_code == 422


# =====================================================================
# 9. 整合：未认证统一返回 401
# =====================================================================


class TestUnauthorizedAccess:
    """所有 /api/support/* 与 /api/onboarding POST 端点未认证返回 401"""

    def test_post_support_tickets_unauthorized(self, client):
        r = client.post(
            "/api/support/tickets",
            json={"category": "咨询", "priority": "普通", "subject": "x", "description": "y"},
        )
        assert r.status_code == 401

    def test_post_onboarding_unauthorized(self, client):
        r = client.post(
            "/api/onboarding",
            json={"relationship": "亲属", "location": "北京", "consent": True},
        )
        assert r.status_code == 401

    def test_post_reply_unauthorized(self, client):
        r = client.post("/api/support/tickets/tkt-nonexistent/replies", json={"content": "x"})
        assert r.status_code == 401
