"""To B 多租户隔离测试（B2B-IMPLEMENTATION Step 4 验收）

覆盖：
  - TenantMiddleware：multi 模式下按 JWT 绑定 tenant_id / org_role；
    single 模式恒走默认租户（路径与现状完全一致）。
  - resolve_tenant_path strict 钩子：multi 模式且无 TenantContext 时抛
    RuntimeError（防止静默落到 default 租户造成数据串扰）。
  - 业务 store 默认目录按租户路由（ending_note / vault / deadman_switch /
    support / onboarding / digital_legacy）。
  - 越权 403：机构 A 成员 token 访问机构 B 数据 → 403；已禁用成员 → 403。

注意：TENANT_MODE / TENANTS_ROOT 在模块导入时读取，测试内 monkeypatch
模块属性而非环境变量，避免 reload 污染其他测试。
"""

from __future__ import annotations

import pytest
from fastapi import Request

from deadman.infrastructure import multi_tenant as mt
from deadman.infrastructure.multi_tenant import (
    DEFAULT_TENANT_ID,
    TenantContext,
    TenantInfo,
    resolve_tenant_path,
)


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    """把多租户模式强制为 multi + 租户根目录重定向到 tmp_path。"""
    monkeypatch.setattr(mt, "TENANT_MODE", "multi")
    monkeypatch.setattr(mt, "TENANTS_ROOT", tmp_path / "tenants")
    return mt


@pytest.fixture
def single_env(monkeypatch):
    """确保 single 模式（默认），避免受其他测试影响。"""
    monkeypatch.setattr(mt, "TENANT_MODE", "single")
    return mt


# =====================================================================
# resolve_tenant_path strict 钩子（无 TenantContext 写库 → RuntimeError）
# =====================================================================


class TestResolveTenantPathStrict:
    def test_multi_no_context_raises(self, multi_env):
        with pytest.raises(RuntimeError, match="TenantContext"):
            resolve_tenant_path("ending_notes", strict=True)

    def test_multi_with_context_ok(self, multi_env, tmp_path):
        with TenantContext(TenantInfo(tenant_id="t1")):
            path = resolve_tenant_path("ending_notes", strict=True)
        assert "t1" in str(path)
        assert "ending_notes" in str(path)

    def test_multi_explicit_tenant_ok(self, multi_env, tmp_path):
        # 显式 tenant_id 不算"缺少上下文"
        path = resolve_tenant_path("vault", tenant_id="t9", strict=True)
        assert "t9" in str(path)

    def test_single_never_raises(self, single_env):
        # single 模式不需要 TenantContext，恒回 ~/.deadman/
        path = resolve_tenant_path("ending_notes", strict=True)
        assert ".deadman" in str(path)
        assert "tenants" not in str(path)

    def test_non_strict_falls_back_to_default_in_multi(self, multi_env, tmp_path):
        # 默认（非 strict）多租户模式无上下文时回退 default 租户（兼容既有调用）
        path = resolve_tenant_path("data/x.json")
        assert DEFAULT_TENANT_ID in str(path)


# =====================================================================
# TenantMiddleware：按 JWT 绑定租户
# =====================================================================


class TestTenantMiddleware:
    @pytest.fixture(autouse=True)
    def _shared_jwt_secret(self, monkeypatch):
        """让签发与 deps.get_jwt_manager 用同一 secret（中间件经 deps 校验）。"""
        from deadman.config import settings

        monkeypatch.setattr(settings, "jwt_secret", "test-secret-tenant-mw-0123456789abcdef")

    def _token(self, tenant_id: str | None = None, org_role: str | None = None) -> str:
        from deadman.auth.jwt import JWTManager

        user = {"user_id": "u1", "email": "u@x.com", "role": "user"}
        return JWTManager(secret="test-secret-tenant-mw-0123456789abcdef").issue(
            user, tenant_id=tenant_id, org_role=org_role
        )

    def _client_with_middleware(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from deadman.web.middleware import TenantMiddleware

        app = FastAPI()

        @app.get("/echo")
        async def echo(request: Request):
            return {
                "tenant_id": request.state.tenant_id,
                "org_role": request.state.org_role,
            }

        app.add_middleware(TenantMiddleware)
        return TestClient(app)

    def test_multi_binds_tenant_from_token(self, multi_env):
        token = self._token(tenant_id="org-1", org_role="case_manager")
        r = self._client_with_middleware().get(
            "/echo", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "org-1"
        assert r.json()["org_role"] == "case_manager"

    def test_multi_no_token_falls_back_default(self, multi_env):
        r = self._client_with_middleware().get("/echo")
        assert r.json().get("tenant_id") == DEFAULT_TENANT_ID
        assert r.json()["org_role"] is None

    def test_multi_bad_token_falls_back_default(self, multi_env):
        r = self._client_with_middleware().get(
            "/echo", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert r.json()["tenant_id"] == DEFAULT_TENANT_ID

    def test_single_always_default_tenant(self, single_env):
        # single 模式：即使 token 带 tenant_id，也恒走默认租户（C 端零迁移）
        token = self._token(tenant_id="org-1", org_role="org_admin")
        r = self._client_with_middleware().get(
            "/echo", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.json()["tenant_id"] == DEFAULT_TENANT_ID
        assert r.json()["org_role"] is None

    def test_middleware_sets_tenant_context_during_request(self, multi_env):
        """中间件进入 TenantContext，业务 store 全程路由到该租户目录。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from deadman.infrastructure.multi_tenant import get_current_tenant_id
        from deadman.web.middleware import TenantMiddleware

        app = FastAPI()

        @app.get("/store-path")
        async def store_path(request: Request):
            from deadman.ending_note.store import EndingNoteStore

            store = EndingNoteStore()
            assert get_current_tenant_id() == request.state.tenant_id
            return {"store_data_dir": str(store.data_dir)}

        app.add_middleware(TenantMiddleware)
        token = self._token(tenant_id="org-a", org_role="case_manager")
        r = TestClient(app).get(
            "/store-path", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200
        assert "org-a" in r.json()["store_data_dir"]
        assert "ending_notes" in r.json()["store_data_dir"]


# =====================================================================
# 业务 store 默认目录按租户路由
# =====================================================================


class TestStoreDefaultDirs:
    def test_ending_note_under_tenant(self, multi_env):
        from deadman.ending_note.store import EndingNoteStore

        with TenantContext(TenantInfo(tenant_id="t1")):
            store = EndingNoteStore()
        assert "t1" in str(store.data_dir)
        assert "ending_notes" in str(store.data_dir)

    def test_vault_under_tenant(self, multi_env):
        from deadman.vault.store import VaultStore

        with TenantContext(TenantInfo(tenant_id="t2")):
            store = VaultStore()
        assert "t2" in str(store.data_dir)
        assert "vault" in str(store.data_dir)

    def test_switch_under_tenant(self, multi_env):
        from deadman.deadman_switch.store import SwitchStore

        with TenantContext(TenantInfo(tenant_id="t3")):
            store = SwitchStore()
        assert "t3" in str(store.data_dir)

    def test_support_under_tenant(self, multi_env):
        from deadman.support.store import TicketStore

        with TenantContext(TenantInfo(tenant_id="t4")):
            store = TicketStore()
        assert "t4" in str(store.data_dir)

    def test_onboarding_under_tenant(self, multi_env):
        from deadman.onboarding.store import OnboardingStore

        with TenantContext(TenantInfo(tenant_id="t5")):
            store = OnboardingStore()
        assert "t5" in str(store.data_dir)

    def test_digital_legacy_under_tenant(self, multi_env):
        from deadman.digital_legacy.store import default_root

        with TenantContext(TenantInfo(tenant_id="t6")):
            root = default_root()
        assert "t6" in str(root)

    def test_single_mode_all_stores_use_legacy_path(self, single_env):
        from deadman.deadman_switch.store import SwitchStore
        from deadman.ending_note.store import EndingNoteStore
        from deadman.support.store import TicketStore
        from deadman.vault.store import VaultStore

        for store in (EndingNoteStore(), VaultStore(), SwitchStore(), TicketStore()):
            assert "tenants" not in str(store.data_dir)
            assert ".deadman" in str(store.data_dir)


# =====================================================================
# 租户间数据隔离（store 层路径级）
# =====================================================================


class TestCrossTenantDataIsolation:
    def test_same_user_different_tenant_no_cross_read(self, multi_env):
        """同一 user_id 在机构 A 写的笔记，机构 B 读不到。"""
        from deadman.ending_note.models import EndingNote
        from deadman.ending_note.store import EndingNoteStore

        with TenantContext(TenantInfo(tenant_id="org-a")):
            store_a = EndingNoteStore()
            store_a.save(EndingNote.new("u1"))

        with TenantContext(TenantInfo(tenant_id="org-b")):
            store_b = EndingNoteStore()
            assert store_b.load("u1") is None  # 跨租户读 → 不存在
            store_b.save(EndingNote.new("u1"))

        with TenantContext(TenantInfo(tenant_id="org-a")):
            assert store_a.load("u1") is not None  # 原租户数据不受影响

    def test_vault_files_under_tenant_dir(self, multi_env):
        """vault 数据落租户目录，机构间目录互不相交。"""
        from deadman.vault.store import VaultStore

        with TenantContext(TenantInfo(tenant_id="org-a")):
            a_dir = VaultStore().data_dir
        with TenantContext(TenantInfo(tenant_id="org-b")):
            b_dir = VaultStore().data_dir
        assert a_dir != b_dir
        assert a_dir.parent.parent == b_dir.parent.parent  # 同一租户根目录
        assert "org-a" in str(a_dir) and "org-b" in str(b_dir)


# =====================================================================
# 越权 403（require_org_role 校验链，复用 org_jwt 测试模式）
# =====================================================================


class TestCrossTenant403:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        from deadman.auth.store import UserStore
        from deadman.config import settings
        from deadman.org import OrgStore

        monkeypatch.setattr(settings, "org_data_dir", tmp_path / "org")
        monkeypatch.setattr(settings, "auth_data_dir", tmp_path / "auth")
        monkeypatch.setattr(settings, "jwt_secret", "test-secret-isolation-0123456789abcdef")
        return {
            "org": OrgStore(data_dir=tmp_path / "org"),
            "auth": UserStore(data_dir=tmp_path / "auth"),
        }

    @pytest.fixture
    def client(self, env):
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from deadman.web.deps import require_org_role

        app = FastAPI()

        @app.get("/protected")
        def protected(ctx: dict = Depends(require_org_role("viewer"))):
            return ctx

        return TestClient(app)

    def _token(self, user_id, tenant_id, org_role):
        from deadman.auth.jwt import JWTManager

        user = {"user_id": user_id, "email": f"{user_id}@x.com", "role": "user"}
        return JWTManager(secret="test-secret-isolation-0123456789abcdef").issue(
            user, tenant_id=tenant_id, org_role=org_role
        )

    def test_org_a_member_cannot_access_org_b(self, client, env):
        """机构 A 成员伪造机构 B 的 token → 机构 B 数据 403。"""
        org_a = env["org"].create_org("A", slug="a")
        org_b = env["org"].create_org("B", slug="b")
        u = env["auth"].register("u@x.com", "password123", "U")
        env["org"].add_member(org_a.org_id, u["user_id"], "case_manager")

        # 用户是 A 的成员，却签发 B 的机构上下文 → B 侧校验失败
        token_b = self._token(u["user_id"], org_b.org_id, "case_manager")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token_b}"})
        assert r.status_code == 403

        # 正确归属 A → 放行
        token_a = self._token(u["user_id"], org_a.org_id, "case_manager")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token_a}"})
        assert r.status_code == 200

    def test_disabled_member_403(self, client, env):
        org = env["org"].create_org("A", slug="a")
        u = env["auth"].register("u@x.com", "password123", "U")
        env["org"].add_member(org.org_id, u["user_id"], "viewer")
        env["org"].set_member_status(org.org_id, u["user_id"], "disabled")
        token = self._token(u["user_id"], org.org_id, "viewer")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_suspended_org_403(self, client, env):
        org = env["org"].create_org("A", slug="a")
        u = env["auth"].register("u@x.com", "password123", "U")
        env["org"].add_member(org.org_id, u["user_id"], "org_admin")
        env["org"].update_org(org.org_id, status="suspended")
        token = self._token(u["user_id"], org.org_id, "org_admin")
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


# =====================================================================
# C 端用户级 store 租户路由（Step 4 缺口补齐）
# =====================================================================


class TestCsideStoreTenantRouting:
    """memory / decedent cases / notification / doc_extract / cron / soul
    默认目录在 multi 模式下按 TenantContext 路由，避免跨租户串扰。"""

    @pytest.mark.parametrize(
        "sub_path,expected",
        [
            ("memory", "memory"),
            ("cases", "cases"),
            ("notifications", "notifications"),
            ("documents", "documents"),
            ("cron", "cron"),
            ("SOUL.md", "SOUL.md"),
        ],
    )
    def test_resolve_tenant_path_covers_cside_dirs(self, multi_env, sub_path, expected):
        with TenantContext(TenantInfo(tenant_id="t-cside")):
            assert resolve_tenant_path(sub_path) == multi_env.TENANTS_ROOT / "t-cside" / expected

    def test_memory_store_routes_by_tenant(self, multi_env):
        from deadman.memory.file_store import FileMemoryStore

        with TenantContext(TenantInfo(tenant_id="t-mem")):
            store = FileMemoryStore()
        assert store.memory_dir == multi_env.TENANTS_ROOT / "t-mem" / "memory"

    def test_shared_knowledge_routes_by_tenant(self, multi_env):
        from deadman.memory.shared_knowledge import SharedKnowledgeStore

        with TenantContext(TenantInfo(tenant_id="t-sk")):
            store = SharedKnowledgeStore()
        assert store.file_path == multi_env.TENANTS_ROOT / "t-sk" / "memory" / "SHARED_KNOWLEDGE.json"

    def test_decedent_registry_routes_by_tenant(self, multi_env):
        from deadman.decedent_id.registry import DecedentRegistry

        with TenantContext(TenantInfo(tenant_id="t-dec")):
            reg = DecedentRegistry()
        assert reg.data_dir == multi_env.TENANTS_ROOT / "t-dec" / "cases"

    def test_notification_guardrail_routes_by_tenant(self, multi_env):
        from deadman.notification.guardrail import NotificationGuardrail

        with TenantContext(TenantInfo(tenant_id="t-notif")):
            guard = NotificationGuardrail()
        assert guard.data_dir == multi_env.TENANTS_ROOT / "t-notif" / "notifications"

    def test_doc_extract_fallback_routes_by_tenant(self, multi_env):
        from deadman.doc_extract.extractor import DocumentExtractor

        with TenantContext(TenantInfo(tenant_id="t-doc")):
            extractor = DocumentExtractor(vault=None)
        assert extractor.data_dir == multi_env.TENANTS_ROOT / "t-doc" / "documents"

    def test_cron_scheduler_routes_by_tenant(self, multi_env):
        from deadman.cron.scheduler import CronScheduler

        with TenantContext(TenantInfo(tenant_id="t-cron")):
            sch = CronScheduler()
        assert sch.data_dir == multi_env.TENANTS_ROOT / "t-cron" / "cron"

    def test_soul_loader_routes_by_tenant(self, multi_env):
        from deadman.soul_loader import SoulLoader

        with TenantContext(TenantInfo(tenant_id="t-soul")):
            loader = SoulLoader()
        assert loader.soul_path == multi_env.TENANTS_ROOT / "t-soul" / "SOUL.md"
