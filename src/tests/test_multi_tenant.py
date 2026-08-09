"""P7.3 多租户隔离测试。"""

from __future__ import annotations

import pytest
from deadman.infrastructure.multi_tenant import (
    DEFAULT_TENANT_ID,
    TenantContext,
    TenantInfo,
    TenantRegistry,
    get_current_tenant,
    get_current_tenant_id,
    resolve_data_path,
    resolve_memory_path,
    resolve_tenant_path,
    resolve_vault_path,
)


@pytest.fixture(autouse=True)
def enable_multi_tenant(monkeypatch, tmp_path):
    """启用 multi_tenant feature flag + 重定向租户根目录到 tmp_path。"""
    monkeypatch.setenv("DEADMAN_MULTI_TENANT_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_TENANTS_ROOT", str(tmp_path / "tenants"))
    # 重新 import 让 TENANTS_ROOT 生效
    import importlib

    import deadman.infrastructure.multi_tenant as mt

    importlib.reload(mt)
    from deadman.infrastructure.feature_flags import get_flags

    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


class TestTenantContext:
    def test_default_tenant_id_when_no_context(self):
        assert get_current_tenant_id() == DEFAULT_TENANT_ID

    def test_context_manager_sets_tenant(self):
        tenant = TenantInfo(tenant_id="t1", name="Acme Corp")
        with TenantContext(tenant):
            assert get_current_tenant_id() == "t1"
            assert get_current_tenant() is tenant

    def test_context_exits_restores_previous(self):
        tenant1 = TenantInfo(tenant_id="t1")
        tenant2 = TenantInfo(tenant_id="t2")
        with TenantContext(tenant1):
            assert get_current_tenant_id() == "t1"
            with TenantContext(tenant2):
                assert get_current_tenant_id() == "t2"
            # 退出 t2 后恢复 t1
            assert get_current_tenant_id() == "t1"

    def test_exception_still_restores(self):
        tenant = TenantInfo(tenant_id="t1")
        with pytest.raises(RuntimeError), TenantContext(tenant):
            raise RuntimeError("boom")
        # 异常后应恢复默认
        assert get_current_tenant_id() == DEFAULT_TENANT_ID


class TestPathResolution:
    def test_resolve_memory_path_with_tenant(self, tmp_path):
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            path = resolve_memory_path("USER.md")
            assert "t1" in str(path)
            assert "memory" in str(path)
            assert str(path).endswith("USER.md")

    def test_resolve_vault_path(self, tmp_path):
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            path = resolve_vault_path("secret.json")
            assert "vault" in str(path)
            assert "t1" in str(path)

    def test_resolve_data_path(self, tmp_path):
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            path = resolve_data_path("config.json")
            assert "data" in str(path)
            assert "t1" in str(path)

    def test_explicit_tenant_id_overrides_context(self, tmp_path):
        """显式 tenant_id 参数优先于 ContextVar。"""
        tenant = TenantInfo(tenant_id="t1")
        with TenantContext(tenant):
            path = resolve_tenant_path("memory/USER.md", tenant_id="t2")
            assert "t2" in str(path)
            assert "t1" not in str(path)

    def test_disabled_flag_uses_default_path(self, monkeypatch):
        """feature flag 关闭时数据落到 ~/.deadman/(向后兼容)。"""
        monkeypatch.setenv("DEADMAN_MULTI_TENANT_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        # 应该落到 ~/.deadman/memory/USER.md
        path = resolve_memory_path("USER.md")
        assert ".deadman" in str(path)
        assert "tenants" not in str(path)


class TestTenantRegistry:
    def test_register_creates_directories(self, tmp_path):
        reg = TenantRegistry(registry_path=tmp_path / "registry.json")
        tenant = TenantInfo(tenant_id="t_new", name="New Tenant", plan="pro")
        reg.register(tenant)

        # 应创建 tenant 数据目录
        import deadman.infrastructure.multi_tenant as mt

        tenant_dir = mt.TENANTS_ROOT / "t_new"
        assert (tenant_dir / "memory").exists()
        assert (tenant_dir / "vault").exists()
        assert (tenant_dir / "data").exists()

    def test_get_returns_registered_tenant(self, tmp_path):
        reg = TenantRegistry(registry_path=tmp_path / "registry.json")
        tenant = TenantInfo(tenant_id="t1", name="Acme")
        reg.register(tenant)
        fetched = reg.get("t1")
        assert fetched is not None
        assert fetched.tenant_id == "t1"
        assert fetched.name == "Acme"

    def test_get_unknown_returns_none(self, tmp_path):
        reg = TenantRegistry(registry_path=tmp_path / "registry.json")
        assert reg.get("nonexistent") is None

    def test_list_tenants(self, tmp_path):
        reg = TenantRegistry(registry_path=tmp_path / "registry.json")
        reg.register(TenantInfo(tenant_id="t1"))
        reg.register(TenantInfo(tenant_id="t2"))
        all_tenants = reg.list_tenants()
        ids = {t.tenant_id for t in all_tenants}
        assert ids == {"t1", "t2"}

    def test_update_tenant_fields(self, tmp_path):
        reg = TenantRegistry(registry_path=tmp_path / "registry.json")
        reg.register(TenantInfo(tenant_id="t1", plan="free"))
        updated = reg.update("t1", plan="pro", quota_token_per_day=500_000)
        assert updated is not None
        assert updated.plan == "pro"
        assert updated.quota_token_per_day == 500_000

    def test_delete_tenant(self, tmp_path):
        reg = TenantRegistry(registry_path=tmp_path / "registry.json")
        reg.register(TenantInfo(tenant_id="t1"))
        assert reg.delete("t1") is True
        assert reg.get("t1") is None
        assert reg.delete("t1") is False  # 再删返回 False

    def test_persist_and_reload(self, tmp_path):
        """注册后新实例能加载。"""
        path = tmp_path / "registry.json"
        reg1 = TenantRegistry(registry_path=path)
        reg1.register(TenantInfo(tenant_id="t1", name="Acme", plan="pro"))

        reg2 = TenantRegistry(registry_path=path)
        fetched = reg2.get("t1")
        assert fetched is not None
        assert fetched.name == "Acme"
        assert fetched.plan == "pro"


class TestIsolation:
    """不同租户数据互不可见。"""

    def test_two_tenants_have_separate_paths(self, tmp_path):
        tenant1 = TenantInfo(tenant_id="t1")
        tenant2 = TenantInfo(tenant_id="t2")

        with TenantContext(tenant1):
            path1 = resolve_memory_path("USER.md")
        with TenantContext(tenant2):
            path2 = resolve_memory_path("USER.md")

        assert path1 != path2
        assert "t1" in str(path1)
        assert "t2" in str(path2)
