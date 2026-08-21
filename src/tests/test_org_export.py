"""机构数据导出测试（B2B-IMPLEMENTATION Step 7.2 验收）

覆盖：
  1. 权限：未认证 401；consultant 无法访问审计导出 / 全量导出（403）
  2. GET /api/org/audit-logs：返回本机构案件事件，含 actor/action/detail
  3. GET /api/org/audit-logs/export?format=csv|json：CSV/JSON 可下载
  4. POST /api/org/export + status + download：全量 zip 仅含本机构数据
  5. 跨机构隔离：B 机构看不到 A 机构的审计与导出
"""

from __future__ import annotations

import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore
from deadman.config import settings
from deadman.org import OrgStore
from deadman.web.routes import org_cases as cases_routes
from deadman.web.routes import org_customers as customers_routes
from deadman.web.routes import org_export as export_routes

SECRET = "test-secret-org-export-0123456789abcdef0123456789abcdef"


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


class _OrgExportFixture:
    def __init__(self, app: FastAPI, env) -> None:
        self.client = TestClient(app)
        self.env = env

    def _user(self, email="u@x.com", role="user") -> dict:
        return self.env["auth"].register(email, "password123", "U")

    def _org(self, name="机构", slug="a"):
        return self.env["org"].create_org(name, slug=slug)

    def _member(self, org, user, org_role="org_admin"):
        self.env["org"].add_member(org.org_id, user["user_id"], org_role)

    def _token(self, user, org, org_role="org_admin") -> str:
        return JWTManager(secret=SECRET).issue(
            user, tenant_id=org.org_id, org_role=org_role
        )

    def _auth(self, user, org, org_role="org_admin") -> dict:
        return {"Authorization": f"Bearer {self._token(user, org, org_role)}"}


def _make_app():
    app = FastAPI()
    app.include_router(customers_routes.router)
    app.include_router(cases_routes.router)
    app.include_router(export_routes.router)
    return app


@pytest.fixture
def api(env):
    fx = _OrgExportFixture(_make_app(), env)
    yield fx


def _setup_org_with_data(api, org_role="org_admin"):
    """建机构 + 用户 + 客户 + 案件（含一次状态变更，产生两条事件）。"""
    user = api._user("m@x.com")
    org = api._org()
    api._member(org, user, org_role)
    h = api._auth(user, org, org_role)
    cust = api.client.post(
        "/api/org/customers", headers=h, json={"display_name": "张三"}
    ).json()
    case = api.client.post(
        "/api/org/cases", headers=h, json={"customer_id": cust["id"]}
    ).json()
    api.client.post(
        f"/api/org/cases/{case['id']}/status",
        headers=h,
        json={"to_status": "in_progress"},
    )
    return user, org, cust, case


# =====================================================================
# 权限矩阵
# =====================================================================
def test_unauthenticated_401(api):
    for method, path in (
        ("GET", "/api/org/audit-logs"),
        ("GET", "/api/org/audit-logs/export"),
        ("POST", "/api/org/export"),
        ("GET", "/api/org/export/status?job_id=x"),
        ("GET", "/api/org/export/x/download"),
    ):
        r = api.client.request(method, path)
        assert r.status_code == 401, (method, path)


def test_consultant_forbidden(api):
    user, org, _, _ = _setup_org_with_data(api, "org_admin")
    h = api._auth(user, org, "consultant")
    assert api.client.get("/api/org/audit-logs", headers=h).status_code == 403
    assert api.client.get("/api/org/audit-logs/export", headers=h).status_code == 403
    assert api.client.post("/api/org/export", headers=h).status_code == 403


# =====================================================================
# 审计日志
# =====================================================================
def test_audit_logs_contain_actor_action_detail(api):
    user, org, _, _ = _setup_org_with_data(api)
    h = api._auth(user, org)
    rows = api.client.get("/api/org/audit-logs", headers=h).json()
    assert rows["count"] == 2
    actions = {e["action"] for e in rows["events"]}
    assert "case.create" in actions
    assert "case.status_change" in actions
    # 审计含 actor / action / detail
    for e in rows["events"]:
        assert e.get("actor_user_id") == user["user_id"]
        assert e.get("action")
        assert isinstance(e.get("detail"), dict)


def test_audit_logs_filter_by_action(api):
    user, org, _, _ = _setup_org_with_data(api)
    h = api._auth(user, org)
    rows = api.client.get(
        "/api/org/audit-logs", headers=h, params={"action": "case.status_change"}
    ).json()
    assert rows["count"] == 1
    assert rows["events"][0]["action"] == "case.status_change"


def test_audit_logs_cross_org_isolation(api):
    _, _, _, _ = _setup_org_with_data(api)
    user_b = api._user("b@x.com")
    org_b = api._org(name="机构B", slug="b")
    api._member(org_b, user_b)
    hb = api._auth(user_b, org_b)
    assert api.client.get("/api/org/audit-logs", headers=hb).json()["count"] == 0


# =====================================================================
# 审计导出
# =====================================================================
def test_audit_export_csv(api):
    user, org, _, _ = _setup_org_with_data(api)
    r = api.client.get("/api/org/audit-logs/export", headers=api._auth(user, org))
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert 'attachment' in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert "action" in text and "case.create" in text and "case.status_change" in text


def test_audit_export_json(api):
    user, org, _, _ = _setup_org_with_data(api)
    r = api.client.get(
        "/api/org/audit-logs/export", headers=api._auth(user, org), params={"fmt": "json"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    assert {e["action"] for e in data["events"]} == {"case.create", "case.status_change"}


def test_audit_export_invalid_format(api):
    user, org, _, _ = _setup_org_with_data(api)
    r = api.client.get(
        "/api/org/audit-logs/export", headers=api._auth(user, org), params={"fmt": "xlsx"}
    )
    assert r.status_code == 400


# =====================================================================
# 全量导出（异步 zip）
# =====================================================================
def test_full_export_zip_contains_only_own_org(api):
    user_a, org_a, cust_a, case_a = _setup_org_with_data(api)
    ha = api._auth(user_a, org_a)

    # 机构 B 的数据不应出现在 A 的导出中
    user_b = api._user("b@x.com")
    org_b = api._org(name="机构B", slug="b")
    api._member(org_b, user_b)
    hb = api._auth(user_b, org_b)
    api.client.post("/api/org/customers", headers=hb, json={"display_name": "李四"})

    job = api.client.post("/api/org/export", headers=ha).json()
    assert job["status"] == "running"

    # 轮询直至 done（后台线程同进程，很快完成）
    for _ in range(100):
        st = api.client.get(
            f"/api/org/export/status?job_id={job['job_id']}", headers=ha
        ).json()
        if st["status"] in ("done", "failed"):
            break
        import time
        time.sleep(0.05)
    assert st["status"] == "done", st

    dl = api.client.get(
        f"/api/org/export/{job['job_id']}/download", headers=ha
    )
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(__import__("io").BytesIO(dl.content)) as zf:
        names = zf.namelist()
        assert "org_export.json" in names and "audit_log.csv" in names
        payload = __import__("json").loads(zf.read("org_export.json"))
        # 仅含本机构数据
        assert payload["org_id"] == org_a.org_id
        assert len(payload["customers"]) == 1
        assert payload["customers"][0]["id"] == cust_a["id"]
        assert len(payload["cases"]) == 1
        assert payload["cases"][0]["id"] == case_a["id"]
        assert len(payload["audit_events"]) == 2
        csv_text = zf.read("audit_log.csv").decode("utf-8-sig")
        assert "case.status_change" in csv_text


def test_full_export_unknown_job_404(api):
    user, org, _, _ = _setup_org_with_data(api)
    r = api.client.get(
        "/api/org/export/status?job_id=nonexistent", headers=api._auth(user, org)
    )
    assert r.status_code == 404
