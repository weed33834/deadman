"""机构工作台页面与聚合端点测试（B2B-IMPLEMENTATION Step 6 验收）

覆盖：
  1. GET /org 返回 org.html（200, text/html）
  2. GET /api/org/dashboard 聚合：客户数/案件数/进行中/我的待办
  3. GET /api/org/kb：平台公共库 + 机构私有库合并视图
  4. POST/DELETE /api/org/kb/{doc_id}：私有知识 CRUD（跨机构 404）
  5. 权限矩阵：viewer 可读仪表盘与知识库；viewer 不可写私有知识（403）；
     org_admin 可看成员/审计；consultant 不可（403）
  6. 未认证 → 401
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore
from deadman.config import settings
from deadman.org import OrgStore
from deadman.web.routes import org_cases as cases_routes
from deadman.web.routes import org_customers as customers_routes
from deadman.web.routes import org_pages as pages_routes

SECRET = "test-secret-org-pages-0123456789abcdef0123456789abcdef"


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


class _OrgApiFixture:
    """组装带 org_pages + customers + cases 路由的临时 FastAPI app。"""

    def __init__(self, app: FastAPI, env) -> None:
        self.client = TestClient(app)
        self.env = env

    def _user(self, email="u@x.com", role="user") -> dict:
        return self.env["auth"].register(email, "password123", "U")

    def _org(self, name="机构", slug="a"):
        return self.env["org"].create_org(name, slug=slug)

    def _member(self, org, user, org_role="case_manager"):
        self.env["org"].add_member(org.org_id, user["user_id"], org_role)

    def _token(self, user, org, org_role="case_manager") -> str:
        return JWTManager(secret=SECRET).issue(
            user, tenant_id=org.org_id, org_role=org_role
        )

    def _auth(self, user, org, org_role="case_manager") -> dict:
        return {"Authorization": f"Bearer {self._token(user, org, org_role)}"}


def _make_app():
    app = FastAPI()
    app.include_router(pages_routes.router)
    app.include_router(customers_routes.router)
    app.include_router(cases_routes.router)
    return app


@pytest.fixture
def api(env):
    fx = _OrgApiFixture(_make_app(), env)
    yield fx


def _setup_org(api, org_role="case_manager"):
    user = api._user("m@x.com")
    org = api._org()
    api._member(org, user, org_role)
    return user, org


# =====================================================================
# 页面路由
# =====================================================================
def test_org_page_served(api):
    r = api.client.get("/org")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "机构工作台" in r.text


# =====================================================================
# 认证 / 权限矩阵
# =====================================================================
def test_unauthenticated_401(api):
    for path in (
        "/api/org/dashboard",
        "/api/org/kb",
        "/api/org/members",
        "/api/org/audit",
    ):
        assert api.client.get(path).status_code == 401, path


def test_viewer_can_read_dashboard_and_kb(api):
    user, org = _setup_org(api, "viewer")
    assert api.client.get("/api/org/dashboard", headers=api._auth(user, org, "viewer")).status_code == 200
    assert api.client.get("/api/org/kb", headers=api._auth(user, org, "viewer")).status_code == 200


def test_viewer_cannot_write_private_kb(api):
    user, org = _setup_org(api, "viewer")
    r = api.client.post(
        "/api/org/kb/foo",
        headers=api._auth(user, org, "viewer"),
        json={"title": "x", "content": "y"},
    )
    assert r.status_code == 403


def test_consultant_cannot_view_members_or_audit(api):
    user, org = _setup_org(api, "consultant")
    assert api.client.get("/api/org/members", headers=api._auth(user, org, "consultant")).status_code == 403
    assert api.client.get("/api/org/audit", headers=api._auth(user, org, "consultant")).status_code == 403


# =====================================================================
# 仪表盘聚合
# =====================================================================
def test_dashboard_aggregates(api):
    user, org = _setup_org(api, "case_manager")
    h = api._auth(user, org, "case_manager")

    # 建客户 + 案件
    cust = api.client.post(
        "/api/org/customers", headers=h, json={"display_name": "张三"}
    ).json()
    case = api.client.post(
        "/api/org/cases",
        headers=h,
        json={"customer_id": cust["id"], "case_type": "funeral"},
    ).json()
    api.client.post(
        f"/api/org/cases/{case['id']}/status",
        headers=h,
        json={"to_status": "in_progress"},
    )

    d = api.client.get("/api/org/dashboard", headers=h).json()
    assert d["customer_count"] == 1
    assert d["case_count"] == 1
    assert d["active_count"] == 1
    assert d["status_breakdown"].get("in_progress") == 1
    assert len(d["recent_cases"]) == 1


def test_dashboard_my_todos(api):
    user, org = _setup_org(api, "case_manager")
    h = api._auth(user, org, "case_manager")

    cust = api.client.post("/api/org/customers", headers=h, json={"display_name": "李四"}).json()
    case = api.client.post(
        "/api/org/cases", headers=h, json={"customer_id": cust["id"]}
    ).json()
    # 分配给他人 → 不计入"我的待办"
    other = api._user("other@x.com")
    api.client.post(
        f"/api/org/cases/{case['id']}/assign",
        headers=h,
        json={"assignee_user_id": other["user_id"]},
    )
    d = api.client.get("/api/org/dashboard", headers=h).json()
    assert d["my_todos"] == 0
    assert d["team_load"].get(other["user_id"]) == 1


# =====================================================================
# 机构知识库
# =====================================================================
def test_kb_merged_view_has_platform(api):
    user, org = _setup_org(api, "case_manager")
    d = api.client.get("/api/org/kb", headers=api._auth(user, org)).json()
    assert "platform" in d and "private" in d
    assert d["private"] == []


def test_kb_crud(api):
    user, org = _setup_org(api, "case_manager")
    h = api._auth(user, org)

    doc = api.client.post(
        "/api/org/kb/guangdong_funeral",
        headers=h,
        json={"title": "广东殡葬SOP", "category": "SOP", "content": "流程一…", "tags": ["广东"]},
    ).json()
    assert doc["id"] == "guangdong_funeral"
    assert doc["org_id"] == org.org_id

    d = api.client.get("/api/org/kb", headers=h).json()
    assert d["private_count"] == 1
    assert d["private"][0]["title"] == "广东殡葬SOP"

    # 更新
    doc2 = api.client.post(
        "/api/org/kb/guangdong_funeral",
        headers=h,
        json={"title": "广东殡葬SOP v2", "category": "SOP", "content": "更新"},
    ).json()
    assert doc2["title"] == "广东殡葬SOP v2"
    assert doc2["created_at"] == doc["created_at"]

    # 删除
    assert api.client.delete("/api/org/kb/guangdong_funeral", headers=h).json()["deleted"] is True
    assert api.client.get("/api/org/kb", headers=h).json()["private"] == []


def test_kb_cross_org_isolation(api):
    user_a, org_a = _setup_org(api, "case_manager")
    ha = api._auth(user_a, org_a)
    api.client.post(
        "/api/org/kb/shared_doc", headers=ha, json={"title": "A机构知识"}
    )

    user_b = api._user("b@x.com")
    org_b = api._org(name="机构B", slug="b")
    api._member(org_b, user_b, "case_manager")
    hb = api._auth(user_b, org_b)
    # B 看不到 A 的知识
    assert api.client.get("/api/org/kb", headers=hb).json()["private"] == []
    # B 无法覆盖 A 的 doc_id（404）
    r = api.client.post(
        "/api/org/kb/shared_doc", headers=hb, json={"title": "篡改"}
    )
    assert r.status_code == 404
    # B 删除 A 的 doc → 404
    assert api.client.delete("/api/org/kb/shared_doc", headers=hb).status_code == 404


# =====================================================================
# 成员与审计
# =====================================================================
def test_members_list(api):
    user, org = _setup_org(api, "org_admin")
    api._member(org, api._user("m2@x.com"), "consultant")
    rows = api.client.get("/api/org/members", headers=api._auth(user, org, "org_admin")).json()
    assert rows["count"] == 2
    assert {m["org_role"] for m in rows["members"]} == {"org_admin", "consultant"}


def test_audit_lists_case_events(api):
    user, org = _setup_org(api, "org_admin")
    h = api._auth(user, org, "case_manager")

    cust = api.client.post("/api/org/customers", headers=h, json={"display_name": "王五"}).json()
    case = api.client.post(
        "/api/org/cases", headers=h, json={"customer_id": cust["id"]}
    ).json()
    api.client.post(
        f"/api/org/cases/{case['id']}/status",
        headers=h,
        json={"to_status": "in_progress"},
    )

    evs = api.client.get(
        "/api/org/audit", headers=api._auth(user, org, "org_admin")
    ).json()
    actions = {e["action"] for e in evs["events"]}
    assert "case.create" in actions
    assert "case.status_change" in actions
