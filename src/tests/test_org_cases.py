"""机构客户/案件 端到端测试（B2B-IMPLEMENTATION Step 5 验收）

覆盖（文件版 + DB 版双轨）：
  1. 全流程：建客户 → 办案 → 分配 → 状态流转 → 材料包 → 归档
  2. 越权 403：跨机构访问 customer/case → 404（双键校验）；角色不足 → 403
  3. 审计完整：状态变更/分配/材料生成强制落 case_events
  4. 案件状态机：CASE_FLOW 非法迁移被拒
  5. repository 双键：跨机构 get 返回 None

文件版默认走（DATABASE_URL 空）；DB 版用 SQLite 内存库（复用 test_db_layer 模式）。
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from deadman.auth.jwt import JWTManager
from deadman.auth.store import UserStore
from deadman.config import settings
from deadman.org import OrgStore
from deadman.org.case_flow import CASE_FLOW, can_transition
from deadman.web.deps import require_org_role
from deadman.web.routes import org_cases as cases_routes
from deadman.web.routes import org_customers as customers_routes

SECRET = "test-secret-org-cases-0123456789abcdef0123456789abcdef"


# =====================================================================
# Fixtures
# =====================================================================
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
    """组装带客户/案件路由的临时 FastAPI app + 机构上下文工具。"""

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
        return JWTManager(secret=SECRET).issue(user, tenant_id=org.org_id, org_role=org_role)

    def _auth(self, user, org, org_role="case_manager") -> dict:
        return {"Authorization": f"Bearer {self._token(user, org, org_role)}"}


def _make_app():
    """注册客户/案件路由的临时 app（不挂全量 app，避免静态资源干扰）。"""
    app = FastAPI()
    app.include_router(customers_routes.router)
    app.include_router(cases_routes.router)
    return app


@pytest.fixture
def api(env):
    return _OrgApiFixture(_make_app(), env)


@pytest.fixture
def api_403(env):
    """只挂 require_org_role 校验用的 /protected（与隔离测试同模式）。"""
    app = FastAPI()

    @app.get("/protected")
    def protected(ctx: dict = Depends(require_org_role("case_manager"))):
        return ctx

    return _OrgApiFixture(app, env)


# =====================================================================
# 1. 案件状态机（纯函数）
# =====================================================================


class TestCaseFlow:
    def test_valid_transitions(self):
        assert can_transition("created", "assigned")
        assert can_transition("created", "in_progress")
        assert can_transition("in_progress", "pending_input")
        assert can_transition("pending_input", "in_progress")
        assert can_transition("closed", "in_progress")  # 重开
        assert can_transition("in_progress", "closed")

    def test_invalid_transitions(self):
        assert not can_transition("created", "pending_input")
        assert not can_transition("created", "closed")
        assert not can_transition("cancelled", "in_progress")
        assert not can_transition("in_progress", "created")
        assert not can_transition("pending_input", "cancelled")

    def test_unknown_state_rejected(self):
        assert not can_transition("hacked", "in_progress")
        assert not can_transition(None, "closed")

    def test_all_statuses_in_flow(self):
        assert set(CASE_FLOW.keys()) == {
            "created",
            "assigned",
            "in_progress",
            "pending_input",
            "closed",
            "cancelled",
        }


# =====================================================================
# 2. 全流程（文件版，DATABASE_URL 空默认）
# =====================================================================


class TestFullJourney:
    def test_create_customer(self, api):
        user = api._user()
        org = api._org()
        api._member(org, user)
        r = api.client.post(
            "/api/org/customers",
            json={"display_name": "张三", "province": "北京", "tags": ["VIP"]},
            headers=api._auth(user, org),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["display_name"] == "张三"
        assert data["province"] == "北京"
        assert data["org_id"] == org.org_id
        assert data["stage"] == "planning"
        assert data["tags"] == ["VIP"]

    def test_create_customer_requires_case_manager(self, api):
        """viewer/consultant 创建客户 → 403（角色不足）。"""
        user = api._user("v@x.com")
        org = api._org("V", "v")
        api._member(org, user, "viewer")
        r = api.client.post(
            "/api/org/customers",
            json={"display_name": "X"},
            headers=api._auth(user, org, "viewer"),
        )
        assert r.status_code == 403

    def test_full_journey(self, api):
        """建客户 → 办案 → 分配 → 状态流转 → 材料包 → 归档。"""
        user = api._user()
        org = api._org()
        api._member(org, user)
        headers = api._auth(user, org)

        # 1) 建客户
        r = api.client.post(
            "/api/org/customers",
            json={"display_name": "李四", "province": "上海"},
            headers=headers,
        )
        customer = r.json()
        assert r.status_code == 200

        # 2) 办案
        r = api.client.post(
            "/api/org/cases",
            json={"customer_id": customer["id"], "case_type": "funeral"},
            headers=headers,
        )
        assert r.status_code == 200
        case = r.json()
        assert case["status"] == "created"
        assert case["customer_id"] == customer["id"]

        # 3) 分配（created → assigned）
        r = api.client.post(
            f"/api/org/cases/{case['id']}/assign",
            json={"assignee_user_id": user["user_id"]},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "assigned"
        assert r.json()["assignee_user_id"] == user["user_id"]

        # 4) 状态流转：assigned → in_progress → pending_input → closed
        for target in ("in_progress", "pending_input", "closed"):
            r = api.client.post(
                f"/api/org/cases/{case['id']}/status",
                json={"to_status": target},
                headers=headers,
            )
            assert r.status_code == 200, f"迁移到 {target} 失败: {r.text}"
            assert r.json()["status"] == target
        assert r.json()["closed_at"] is not None  # 归档时间已落

        # 5) 重开：closed → in_progress
        r = api.client.post(
            f"/api/org/cases/{case['id']}/status",
            json={"to_status": "in_progress"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"

        # 6) 材料包（memorial，LLM 降级为模板填充，不阻塞）
        r = api.client.post(
            f"/api/org/cases/{case['id']}/material",
            json={"generator": "memorial", "doc_type": "epitaph", "decedent_name": "李四"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        material = r.json()["material"]
        assert material["text"]

        # 7) 审计完整：create/assign/status_change×3/material_generate 至少 6 条
        r = api.client.get(f"/api/org/cases/{case['id']}/events", headers=headers)
        assert r.status_code == 200
        events = r.json()["events"]
        actions = {e["action"] for e in events}
        assert "case.create" in actions
        assert "case.assign" in actions
        assert "case.status_change" in actions
        assert "case.material_generate" in actions
        assert len(events) >= 6
        # 所有事件 actor 正确
        assert all(e["actor_user_id"] == user["user_id"] for e in events)

    def test_illegal_status_transition_rejected(self, api):
        user = api._user()
        org = api._org()
        api._member(org, user)
        headers = api._auth(user, org)
        r = api.client.post("/api/org/customers", json={"display_name": "王五"}, headers=headers)
        customer = r.json()
        r = api.client.post(
            "/api/org/cases",
            json={"customer_id": customer["id"]},
            headers=headers,
        )
        case = r.json()
        # created → pending_input 非法
        r = api.client.post(
            f"/api/org/cases/{case['id']}/status",
            json={"to_status": "pending_input"},
            headers=headers,
        )
        assert r.status_code == 400
        assert "不合法" in r.json()["detail"]

    def test_customer_profile_aggregates(self, api):
        user = api._user()
        org = api._org()
        api._member(org, user)
        headers = api._auth(user, org)
        r = api.client.post("/api/org/customers", json={"display_name": "赵六"}, headers=headers)
        customer = r.json()
        r = api.client.post(
            "/api/org/cases",
            json={"customer_id": customer["id"]},
            headers=headers,
        )
        case = r.json()
        r = api.client.get(f"/api/org/customers/{customer['id']}/profile", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["case_count"] == 1
        assert data["cases"][0]["id"] == case["id"]
        assert data["stage_summary"].get("created") == 1

    def test_delete_customer_requires_org_admin(self, api):
        user = api._user()
        org = api._org()
        api._member(org, user, "case_manager")
        headers = api._auth(user, org)
        r = api.client.post(
            "/api/org/customers", json={"display_name": "删除测试"}, headers=headers
        )
        customer = r.json()
        # case_manager 无权删除 → 403
        r = api.client.delete(f"/api/org/customers/{customer['id']}", headers=headers)
        assert r.status_code == 403
        # org_admin 可删
        admin = api._user("admin@x.com")
        api._member(org, admin, "org_admin")
        r = api.client.delete(
            f"/api/org/customers/{customer['id']}",
            headers=api._auth(admin, org, "org_admin"),
        )
        assert r.status_code == 200


# =====================================================================
# 3. 越权 403 / 跨机构 404
# =====================================================================


class TestCrossTenantAccess:
    def test_org_a_member_cannot_read_org_b_customer(self, api):
        """机构 A 成员直接访问机构 B 的 customer id → 404（双键校验）。"""
        user_a = api._user("a@x.com")
        org_a = api._org("A", "a")
        api._member(org_a, user_a)
        user_b = api._user("b@x.com")
        org_b = api._org("B", "b")
        api._member(org_b, user_b)
        headers_b = api._auth(user_b, org_b)

        # B 创建客户
        r = api.client.post("/api/org/customers", json={"display_name": "B客户"}, headers=headers_b)
        customer_b = r.json()

        # A 的 token 读 B 的客户 → 404
        r = api.client.get(
            f"/api/org/customers/{customer_b['id']}",
            headers=api._auth(user_a, org_a),
        )
        assert r.status_code == 404

        # A 更新 B 客户 → 404
        r = api.client.patch(
            f"/api/org/customers/{customer_b['id']}",
            json={"stage": "funeral"},
            headers=api._auth(user_a, org_a),
        )
        assert r.status_code == 404

    def test_org_a_member_cannot_read_org_b_case(self, api):
        user_a = api._user("a@x.com")
        org_a = api._org("A", "a")
        api._member(org_a, user_a)
        user_b = api._user("b@x.com")
        org_b = api._org("B", "b")
        api._member(org_b, user_b)
        headers_b = api._auth(user_b, org_b)

        r = api.client.post("/api/org/customers", json={"display_name": "B客户"}, headers=headers_b)
        customer_b = r.json()
        r = api.client.post(
            "/api/org/cases",
            json={"customer_id": customer_b["id"]},
            headers=headers_b,
        )
        case_b = r.json()

        # A 读 B 的案件 → 404
        r = api.client.get(f"/api/org/cases/{case_b['id']}", headers=api._auth(user_a, org_a))
        assert r.status_code == 404

        # A 改 B 案件状态 → 404
        r = api.client.post(
            f"/api/org/cases/{case_b['id']}/status",
            json={"to_status": "in_progress"},
            headers=api._auth(user_a, org_a),
        )
        assert r.status_code == 404

        # A 读 B 案件事件 → 空（双键过滤，不泄漏）
        r = api.client.get(
            f"/api/org/cases/{case_b['id']}/events",
            headers=api._auth(user_a, org_a),
        )
        assert r.status_code == 200
        assert r.json()["events"] == []

    def test_no_token_401(self, api):
        assert api.client.get("/api/org/customers").status_code == 401
        assert api.client.get("/api/org/cases").status_code == 401

    def test_fake_org_context_403(self, api_403):
        """用户伪造其它机构 tenant_id（非成员）→ require_org_role 403。"""
        u = api_403.env["auth"].register("u@x.com", "password123", "U")
        oa = api_403.env["org"].create_org("A", slug="a")
        ob = api_403.env["org"].create_org("B", slug="b")
        api_403.env["org"].add_member(oa.org_id, u["user_id"], "case_manager")
        # 伪造 B 机构上下文（用户不是 B 成员）
        token = JWTManager(secret=SECRET).issue(u, tenant_id=ob.org_id, org_role="case_manager")
        r = api_403.client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


# =====================================================================
# 4. Repository 层（文件版 + DB 版）
# =====================================================================


class TestFileRepositories:
    async def test_customer_crud_and_org_scope(self, tmp_path):
        from deadman.org.file_customers import (
            CustomerRepository,
        )

        repo = CustomerRepository(data_dir=tmp_path)
        c = await repo.create("org-a", {"display_name": "客户A"}, actor_user_id="u1")
        await repo.create("org-b", {"display_name": "客户B"}, actor_user_id="u1")
        # 双键：跨机构 get None
        assert await repo.get("org-a", c["id"]) == c
        assert await repo.get("org-b", c["id"]) is None
        assert len(await repo.list_by_org("org-a")) == 1
        # 更新/删除越权保护
        assert await repo.update("org-b", c["id"], {"stage": "done"}) is None
        assert await repo.delete("org-b", c["id"]) is False
        assert await repo.delete("org-a", c["id"]) is True

    async def test_case_flow_and_events(self, tmp_path):
        from deadman.org.file_customers import (
            CaseEventRepository,
            CaseRepository,
            CustomerRepository,
        )

        cust = CustomerRepository(data_dir=tmp_path)
        repo = CaseRepository(data_dir=tmp_path)
        events = CaseEventRepository(data_dir=tmp_path)
        c = await cust.create("org-a", {"display_name": "C"}, actor_user_id="u1")
        case = await repo.create("org-a", {"customer_id": c["id"]}, actor_user_id="u1")
        # 非法迁移
        with pytest.raises(ValueError):
            await repo.update_status("org-a", case["id"], "pending_input", "u1")
        # 合法流转
        await repo.assign("org-a", case["id"], "u2", actor_user_id="u1")
        await repo.update_status("org-a", case["id"], "in_progress", "u1")
        await repo.update_status("org-a", case["id"], "closed", "u1")
        rows = await events.list_by_case("org-a", case["id"])
        actions = {e["action"] for e in rows}
        assert {"case.create", "case.assign", "case.status_change"} <= actions
        # 事件跨机构不可见
        assert await events.list_by_case("org-b", case["id"]) == []


class TestDbRepositories:
    """DB 版 repository：SQLite 内存库。"""

    @pytest.fixture
    async def sqlite_db(self, monkeypatch):
        from deadman.db.engine import dispose_engine

        old_url = settings.database_url
        settings.database_url = "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true"
        await dispose_engine()
        yield settings
        await dispose_engine()
        settings.database_url = old_url

    @pytest.fixture
    async def initialized_db(self, sqlite_db):
        from deadman.db.engine import init_db

        await init_db()
        return sqlite_db

    async def test_db_repo_full_flow(self, initialized_db):
        from deadman.db.repositories import (
            CaseEventRepository,
            CaseRepository,
            CustomerRepository,
        )

        cust = CustomerRepository()
        repo = CaseRepository()
        events = CaseEventRepository()

        c = await cust.create("org-a", {"display_name": "DB客户"}, actor_user_id="u1")
        assert c["org_id"] == "org-a"
        # 双键隔离
        assert await cust.get("org-b", c["id"]) is None
        assert (await cust.count_by_org("org-a")) == 1

        case = await repo.create("org-a", {"customer_id": c["id"]}, actor_user_id="u1")
        assert case["status"] == "created"
        # 跨机构访问 → None
        assert await repo.get("org-b", case["id"]) is None

        await repo.assign("org-a", case["id"], "u2", actor_user_id="u1")
        await repo.update_status("org-a", case["id"], "in_progress", "u1")
        await repo.update_status("org-a", case["id"], "closed", "u1")

        # 非法迁移拒绝
        with pytest.raises(ValueError):
            await repo.update_status("org-a", case["id"], "cancelled", "u1")

        rows = await events.list_by_case("org-a", case["id"])
        actions = {e["action"] for e in rows}
        assert {"case.create", "case.assign", "case.status_change"} <= actions
        # 事件跨机构不可见
        assert await events.list_by_case("org-b", case["id"]) == []

        # 案件计数
        assert (await repo.count_by_org("org-a", status="closed")) == 1
        assert (await repo.count_by_org("org-a", status="created")) == 0

    async def test_db_repo_validate_transition(self, initialized_db):
        from deadman.db.repositories import CaseRepository, CustomerRepository

        cust = CustomerRepository()
        repo = CaseRepository()
        c = await cust.create("org-a", {"display_name": "D"}, actor_user_id="u1")
        case = await repo.create("org-a", {"customer_id": c["id"]}, actor_user_id="u1")
        with pytest.raises(ValueError, match="不合法"):
            await repo.update_status("org-a", case["id"], "closed", "u1")
