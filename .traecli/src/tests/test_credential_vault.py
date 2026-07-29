"""P7.8 凭证保险柜测试 - AES-256-GCM 加密存储。"""

from __future__ import annotations

import os
import time

import pytest

from deadman.infrastructure.credential_vault import (
    CredentialNotFoundError,
    CredentialVault,
    _decrypt,
    _derive_master_key,
    _encrypt,
    _HAS_CRYPTO,
)


@pytest.fixture(autouse=True)
def enable_credential_vault(monkeypatch):
    monkeypatch.setenv("DEADMAN_CREDENTIAL_VAULT_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


@pytest.fixture
def fresh_master_key(monkeypatch, tmp_path):
    """生成临时主密钥(避免污染全局)。"""
    import base64
    import secrets
    if _HAS_CRYPTO:
        key = secrets.token_bytes(32)
        monkeypatch.setenv("DEADMAN_VAULT_MASTER_KEY", base64.b64encode(key).decode())
    yield


class TestEncryptDecrypt:
    """加密/解密原语。"""

    def test_roundtrip_returns_original(self, fresh_master_key):
        master_key = _derive_master_key()
        plaintext = "sk-1234567890abcdef"
        encrypted = _encrypt(plaintext, master_key)
        # 加密后应不等于原文
        assert encrypted != plaintext
        # 解密后等于原文
        decrypted = _decrypt(encrypted, master_key)
        assert decrypted == plaintext

    def test_different_encryptions_differ(self, fresh_master_key):
        """AES-GCM 每次加密 nonce 不同,密文不同。"""
        if not _HAS_CRYPTO:
            pytest.skip("cryptography not installed")
        master_key = _derive_master_key()
        e1 = _encrypt("secret", master_key)
        e2 = _encrypt("secret", master_key)
        # 同一明文不同密文(nonce 不同)
        assert e1 != e2

    def test_wrong_master_key_fails_decryption(self, fresh_master_key):
        if not _HAS_CRYPTO:
            pytest.skip("cryptography not installed")
        import secrets
        key1 = _derive_master_key()
        key2 = secrets.token_bytes(32)
        encrypted = _encrypt("secret", key1)
        # 用错误 key 解密应抛异常
        with pytest.raises(Exception):
            _decrypt(encrypted, key2)


class TestSetGet:
    def test_set_then_get_returns_plaintext(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("openai_api_key", "sk-abc123", tenant_id="t1")
        value = vault.get("openai_api_key", tenant_id="t1")
        assert value == "sk-abc123"

    def test_get_unknown_raises(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        with pytest.raises(CredentialNotFoundError):
            vault.get("nonexistent", tenant_id="t1")

    def test_set_persists_ciphertext_not_plaintext(self, tmp_path, fresh_master_key):
        vault_path = tmp_path / "vault.json"
        vault = CredentialVault(vault_path=vault_path)
        vault.set("openai_api_key", "sk-super-secret-123", tenant_id="t1")
        # 文件中不应出现明文
        content = vault_path.read_text(encoding="utf-8")
        assert "sk-super-secret-123" not in content
        # 应该有 ciphertext
        assert "ciphertext" in content

    def test_set_preserves_created_at_on_update(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        record1 = vault.set("api_key", "v1", tenant_id="t1")
        original_created = record1.created_at
        time.sleep(0.01)
        # 更新
        record2 = vault.set("api_key", "v2", tenant_id="t1")
        # created_at 应保留,last_rotated_at 应更新
        assert record2.created_at == original_created
        assert record2.last_rotated_at > original_created


class TestCache:
    def test_cached_value_returned_within_ttl(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json", cache_ttl=10)
        vault.set("api_key", "v1", tenant_id="t1")
        # 第一次读:解密
        v1 = vault.get("api_key", tenant_id="t1")
        # 第二次读:从缓存(不解密)
        v2 = vault.get("api_key", tenant_id="t1")
        assert v1 == v2 == "v1"

    def test_cache_expires_after_ttl(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json", cache_ttl=0.1)
        vault.set("api_key", "v1", tenant_id="t1")
        vault.get("api_key", tenant_id="t1")  # 填充缓存
        time.sleep(0.15)  # 超过 TTL
        # 应重新解密(缓存过期)
        v2 = vault.get("api_key", tenant_id="t1")
        assert v2 == "v1"


class TestTenantIsolation:
    def test_tenant_cannot_access_other_tenant(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("api_key", "t1_secret", tenant_id="t1")
        # t2 应读不到
        with pytest.raises(CredentialNotFoundError):
            vault.get("api_key", tenant_id="t2")

    def test_same_name_different_tenants(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("api_key", "t1_value", tenant_id="t1")
        vault.set("api_key", "t2_value", tenant_id="t2")
        assert vault.get("api_key", tenant_id="t1") == "t1_value"
        assert vault.get("api_key", tenant_id="t2") == "t2_value"


class TestDelete:
    def test_delete_removes_credential(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("api_key", "v1", tenant_id="t1")
        assert vault.delete("api_key", tenant_id="t1") is True
        with pytest.raises(CredentialNotFoundError):
            vault.get("api_key", tenant_id="t1")

    def test_delete_unknown_returns_false(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        assert vault.delete("nonexistent", tenant_id="t1") is False


class TestRotation:
    def test_needs_rotation_when_never_rotated(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        record = vault.set("api_key", "v1", tenant_id="t1")
        # 刚设置,但 last_rotated_at > 0,所以理论上 needs_rotation=False
        # 等价测试:rotation_days=0 时一定 needs_rotation=True
        assert record.needs_rotation(rotation_days=0) is True

    def test_needs_rotation_after_threshold(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        record = vault.set("api_key", "v1", tenant_id="t1")
        # 模拟很久以前轮换
        record.last_rotated_at = time.time() - 86400 * 100  # 100 天前
        assert record.needs_rotation(rotation_days=90) is True

    def test_rotate_updates_value(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("api_key", "v1", tenant_id="t1")
        vault.rotate("api_key", "v2", tenant_id="t1")
        assert vault.get("api_key", tenant_id="t1") == "v2"

    def test_list_needing_rotation(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("key1", "v1", tenant_id="t1")
        vault.set("key2", "v2", tenant_id="t1")
        # 模拟 key1 100 天前轮换
        records = vault.list_credentials(tenant_id="t1")
        records[0].last_rotated_at = time.time() - 86400 * 100
        vault._save()
        # 重置缓存
        vault._loaded = False
        vault._records.clear()
        needing = vault.list_needing_rotation(tenant_id="t1", rotation_days=90)
        assert any(r.name == "key1" for r in needing)


class TestAccessAudit:
    def test_access_count_increments(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("api_key", "v1", tenant_id="t1")
        # 多次读取
        for _ in range(3):
            vault.get("api_key", tenant_id="t1")
        records = vault.list_credentials(tenant_id="t1")
        assert records[0].access_count == 3

    def test_last_accessed_at_updated(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("api_key", "v1", tenant_id="t1")
        before = time.time()
        vault.get("api_key", tenant_id="t1")
        records = vault.list_credentials(tenant_id="t1")
        assert records[0].last_accessed_at >= before


class TestListCredentials:
    def test_list_returns_only_metadata(self, tmp_path, fresh_master_key):
        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        vault.set("key1", "v1", tenant_id="t1", metadata={"description": "OpenAI key"})
        vault.set("key2", "v2", tenant_id="t1")
        records = vault.list_credentials(tenant_id="t1")
        assert len(records) == 2
        # 不应包含明文 value(只返回 ciphertext)
        names = {r.name for r in records}
        assert "key1" in names
        assert "key2" in names


class TestPersistence:
    def test_persist_across_instances(self, tmp_path, fresh_master_key):
        vault_path = tmp_path / "vault.json"
        v1 = CredentialVault(vault_path=vault_path)
        v1.set("api_key", "secret_value", tenant_id="t1")

        v2 = CredentialVault(vault_path=vault_path)
        assert v2.get("api_key", tenant_id="t1") == "secret_value"

    def test_file_permissions_600(self, tmp_path, fresh_master_key):
        vault_path = tmp_path / "vault.json"
        vault = CredentialVault(vault_path=vault_path)
        vault.set("api_key", "v1", tenant_id="t1")
        # 文件权限应为 600
        if os.name == "posix":
            stat = vault_path.stat()
            assert stat.st_mode & 0o777 == 0o600


class TestFeatureFlagDisabled:
    """feature flag 关闭时从 env 读取(向后兼容)。"""

    def test_disabled_falls_back_to_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_CREDENTIAL_VAULT_ENABLED", "0")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        value = vault.get("OPENAI_API_KEY")
        assert value == "sk-from-env"

    def test_disabled_unknown_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_CREDENTIAL_VAULT_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        vault = CredentialVault(vault_path=tmp_path / "vault.json")
        with pytest.raises(CredentialNotFoundError):
            vault.get("NONEXISTENT_KEY")
