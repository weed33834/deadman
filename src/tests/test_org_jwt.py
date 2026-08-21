"""To B 机构上下文测试（B2B-IMPLEMENTATION Step 3 验收）

覆盖：
  - JWT issue/verify/switch_org/refresh 的机构上下文往返
  - web.deps.require_org_role 校验链（401/403 分支）
  - web.deps.require_admin strict 修复（role=="admin"，替代不存在的 is_admin 字段）
  - /api/orgs/switch 集成（临时 FastAPI app）

隔离：monkeypatch settings（org_data_dir / auth_data_dir / jwt_secret），
不污染 ~/.deadman；签发用固定 SECRET 与 deps.get_jwt_manager 保持一致。
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore
from deadman.config import settings
from deadman.org import OrgStore
from deadman.web.deps import require_admin, require_org_role
from deadman.web.routes import orgs as orgs_routes

SECRET = "test-secret-org-jwt-0123456789abcdef0123456789abcdef"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """隔离 settings 目录 + 构建 OrgStore/UserStore。"""
    monkeypatch.setattr(settings, "org_data_dir", tmp_path / "org")
    monkeypatch.setattr(settings, "auth_data_dir", tmp_path / "auth")
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    return {
        "org": OrgStore(data_dir=tmp_path / "org"),
        "auth": UserStore(data_dir=tmp_path / "auth"),
    }


# =====================================================================
# JWT 单元：签发/校验/切换/刷新
# =====================================================================


class TestJwtOrgContext:
    def _mgr(self) -> JWTManager:
        return JWTManager(secret=SECRET, expiry_days=7)

    def test_issue_without_org_tenant_none(self):
        token = self._mgr().issue({"user_id": "u1", "email": "a@b.com", "role": "user"})
        payload = self._mgr().verify(token)
        assert payload["tenant_id"] is None
        assert payload["org_role"] is None

    def test_issue_with_org_roundtrip(self):
        mgr = self._mgr()
        token = mgr.issue(
            {"user_id": "u1", "email": "a@b.com", "role": "user"},
            tenant_id="org-1",
            org_role="case_manager",
        )
        payload = mgr.verify(token)
        assert payload["tenant_id"] == "org-1"
        assert payload["org_role"] == "case_manager"
        assert payload["role"] == "user"  # 平台层角色与机构角色双轨

    def test_switch_org_changes_payload(self):
        mgr = self._mgr()
        user = {"user_id": "u1", "email": "a@b.com", "role": "user"}
        t1 = mgr.issue(user, tenant_id="org-1", org_role="viewer")
        t2 = mgr.switch_org(user, "org-2", "org_admin")
        p1, p2 = mgr.verify(t1), mgr.verify(t2)
        assert p1["tenant_id"] == "org-1" and p1["org_role"] == "viewer"
        assert p2["tenant_id"] == "org-2" and p2["org_role"] == "org_admin"

    def test_refresh_preserves_tenant_context(self):
        mgr = JWTManager(secret=SECRET, expiry_days=1)
        payload = {
            "user_id": "u1",
            "email": "a@b.com",
            "role": "user",
            "tenant_id": "org-1",
            "org_role": "consultant",
            "iat": int(time.time()) - 23 * 3600,
            "exp": int(time.time()) + 1 * 3600,
        }
        stale = pyjwt.encode(payload, SECRET, algorithm="HS256")
        refreshed = mgr.refresh(stale)
        assert refreshed is not None
        rp = mgr.verify(refreshed)
        assert rp["tenant_id"] == "org-1"
        assert rp["org_role"] == "consultant"


# =====================================================================
# require_org_role 校验链
# =====================================================================


class TestRequireOrgRole:
    @pytest.fixture
    def client(self, env):
        app = FastAPI()

        @app.get("/protected")
        def protected(ctx: dict = Depends(require_org_role("case_manager"))):
            return ctx

        @app.get("/viewable")
        def viewable(ctx: dict = Depends(require_org_role("viewer"))):
            return ctx

        return TestClient(app)

    def _token(self, user_id="u1", tenant_id="org-1", org_role="case_manager") -> str:
        user = {"user_id": user_id, "email": f"{user_id}@x.com", "role": "user"}
        return JWTManager(secret=SECRET).issue(user, tenant_id=tenant_id, org_role=org_role)

    def test_no_token_401(self, client):
        assert client.get("/protected").status_code == 401

    def test_token_without_org_403(self, client, env):
        user = {"user_id": "u1", "email": "u@x.com", "role": "user"}
        token = JWTManager(secret=SECRET).issue(user)
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_unknown_org_403(self, client, env):
        token = self._token(tenant_id="no-such-org", org_role="org_admin")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_unknown_role_403(self, client, env):
        org = env["org"].create_org("A", slug="a")
        env["org"].add_member(org.org_id, "u1", "viewer")
        token = self._token(tenant_id=org.org_id, org_role="hacker")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_insufficient_role_403(self, client, env):
        org = env["org"].create_org("A", slug="a")
        env["org"].add_member(org.org_id, "u1", "viewer")
        token = self._token(tenant_id=org.org_id, org_role="viewer")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_sufficient_role_200(self, client, env):
        org = env["org"].create_org("A", slug="a")
        env["org"].add_member(org.org_id, "u1", "case_manager")
        token = self._token(tenant_id=org.org_id, org_role="case_manager")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["tenant_id"] == org.org_id
        assert data["org_role"] == "case_manager"
        assert data["org"]["slug"] == "a"

    def test_org_suspended_403(self, client, env):
        org = env["org"].create_org("A", slug="a")
        env["org"].add_member(org.org_id, "u1", "org_admin")
        env["org"].update_org(org.org_id, status="suspended")
        token = self._token(tenant_id=org.org_id, org_role="org_admin")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_membership_disabled_403(self, client, env):
        org = env["org"].create_org("A", slug="a")
        env["org"].add_member(org.org_id, "u1", "case_manager")
        env["org"].set_member_status(org.org_id, "u1", "disabled")
        token = self._token(tenant_id=org.org_id, org_role="case_manager")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_viewer_allowed_for_viewable(self, client, env):
        org = env["org"].create_org("A", slug="a")
        env["org"].add_member(org.org_id, "u1", "viewer")
        token = self._token(tenant_id=org.org_id, org_role="viewer")
        r = client.get("/viewable", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


# =====================================================================
# require_admin strict 修复（role == "admin"）
# =====================================================================


class TestRequireAdminStrict:
    """直接调用 require_admin 依赖函数（绕过 conftest 注入的 X-Admin-Token 头）。"""

    def _token(self, user_id: str, role: str) -> str:
        user = {"user_id": user_id, "email": f"{user_id}@x.com", "role": role}
        return JWTManager(secret=SECRET).issue(user)

    def _call(self, token: str, strict: bool = True):
        cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        return require_admin(cred=cred, x_admin_token=None, strict=strict)

    def test_normal_user_strict_403(self, env):
        u = env["auth"].register("u1@x.com", "password123", "U1")
        token = self._token(u["user_id"], "user")
        with pytest.raises(HTTPException) as exc:
            self._call(token)
        assert exc.value.status_code == 403

    def test_admin_user_strict_200(self, env):
        u = env["auth"].register("a1@x.com", "password123", "A1")
        env["auth"].update_user(u["user_id"], {"role": "admin"})
        token = self._token(u["user_id"], "admin")
        result = self._call(token)
        assert result["role"] == "admin"
        assert result["source"] == "jwt"

    def test_admin_user_non_strict_200_for_normal_user(self, env):
        u = env["auth"].register("u1@x.com", "password123", "U1")
        token = self._token(u["user_id"], "user")
        result = self._call(token, strict=False)
        assert result["source"] == "jwt"

    def test_no_token_401(self):
        with pytest.raises(HTTPException) as exc:
            require_admin(cred=None, x_admin_token=None, strict=True)
        assert exc.value.status_code == 401

    def test_bad_admin_token_401(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_ADMIN_TOKEN", "real-token")
        with pytest.raises(HTTPException) as exc:
            require_admin(cred=None, x_admin_token="wrong-token", strict=True)
        assert exc.value.status_code == 401


# =====================================================================
# /api/orgs/switch + memberships 集成
# =====================================================================


class TestOrgSwitchEndpoint:
    @pytest.fixture
    def client(self, env):
        app = FastAPI()
        app.include_router(orgs_routes.router)
        return TestClient(app)

    def _token(self, user_id: str, role: str = "user") -> str:
        user = {"user_id": user_id, "email": f"{user_id}@x.com", "role": role}
        return JWTManager(secret=SECRET).issue(user)

    def test_switch_success(self, client, env):
        u = env["auth"].register("u1@x.com", "password123", "U1")
        org = env["org"].create_org("测试机构", slug="test-a")
        env["org"].add_member(org.org_id, u["user_id"], "case_manager")
        token = self._token(u["user_id"])
        r = client.post(
            "/api/orgs/switch",
            json={"org_id": org.org_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["tenant_id"] == org.org_id
        assert data["org_role"] == "case_manager"
        assert data["org_name"] == "测试机构"
        payload = JWTManager(secret=SECRET).verify(data["token"])
        assert payload["tenant_id"] == org.org_id
        assert payload["org_role"] == "case_manager"

    def test_switch_not_member_403(self, client, env):
        u = env["auth"].register("u1@x.com", "password123", "U1")
        org = env["org"].create_org("B", slug="b")
        token = self._token(u["user_id"])
        r = client.post(
            "/api/orgs/switch",
            json={"org_id": org.org_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_switch_unknown_org_404(self, client, env):
        u = env["auth"].register("u1@x.com", "password123", "U1")
        token = self._token(u["user_id"])
        r = client.post(
            "/api/orgs/switch",
            json={"org_id": "no-such"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404

    def test_switch_requires_auth_401(self, client, env):
        r = client.post("/api/orgs/switch", json={"org_id": "x"})
        assert r.status_code == 401

    def test_memberships(self, client, env):
        u = env["auth"].register("u1@x.com", "password123", "U1")
        o1 = env["org"].create_org("A", slug="a")
        o2 = env["org"].create_org("B", slug="b")
        env["org"].add_member(o1.org_id, u["user_id"], "viewer")
        env["org"].add_member(o2.org_id, u["user_id"], "org_admin")
        token = self._token(u["user_id"])
        r = client.get("/api/orgs/memberships", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2

    def test_me_returns_tenant_context(self, client, env):
        user = {"user_id": "u1", "email": "u1@x.com", "role": "user"}
        token = JWTManager(secret=SECRET).issue(user)
        r = client.get("/api/orgs/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["tenant_id"] is None
