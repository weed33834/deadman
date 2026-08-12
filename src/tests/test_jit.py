"""P5.2 JIT 短时工具权限 - 测试矩阵

覆盖点：
1. test_jit_grant_returns_token: 发放 token 返回 JITToken
2. test_jit_verify_valid_token: 有效 token 验证通过
3. test_jit_verify_expired_token_fails: 过期 token 验证失败
4. test_jit_revoke_invalidates: 撤销后 token 失效
5. test_jit_cleanup_expired: 清理过期 token
6. test_jit_disabled_noop: feature flag 关闭行为不变
7. test_jit_verify_wrong_tool_fails: 工具名不匹配验证失败
8. test_jit_verify_wrong_scope_fails: scope 不匹配验证失败
9. test_jit_persistence_roundtrip: 持久化往返
10. test_jit_global_singleton: 全局单例
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import deadman.security.jit as jit_module
from deadman.security.jit import (
    DEFAULT_TTL_SECONDS,
    JITPermissionManager,
    JITToken,
    get_jit_manager,
    reset_jit_manager,
)

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_jit(monkeypatch):
    """每个测试默认开启 jit feature flag"""
    monkeypatch.setattr(jit_module, "JIT_PERMISSION_ENABLED", True)
    reset_jit_manager()
    yield
    reset_jit_manager()


@pytest.fixture
def tmp_jit_path(tmp_path) -> Path:
    """临时 jit token 文件路径"""
    return tmp_path / "jit_tokens.json"


@pytest.fixture
def manager(tmp_jit_path) -> JITPermissionManager:
    """构造一个用临时路径的 manager"""
    return JITPermissionManager(persist_path=tmp_jit_path)


# =====================================================================
# 1. grant 返回 token
# =====================================================================


class TestJitGrantReturnsToken:
    def test_jit_grant_returns_token(self, manager):
        """grant 返回 JITToken，字段完整"""
        token = manager.grant(
            tool_name="write_file",
            scope="write",
            ttl_seconds=300,
            granted_to="agent.legal_advisor",
        )
        assert token is not None
        assert token.token  # 非空字符串
        assert token.tool_name == "write_file"
        assert token.scope == "write"
        assert token.granted_to == "agent.legal_advisor"
        assert token.expires_at > token.granted_at
        # expires_at = granted_at + 300
        assert abs(token.expires_at - token.granted_at - 300) < 2
        # token 字符串足够长（secrets.token_urlsafe(32)）
        assert len(token.token) >= 32

    def test_jit_grant_default_ttl(self, manager):
        """不指定 ttl 用默认 DEFAULT_TTL_SECONDS"""
        token = manager.grant(tool_name="read_file")
        assert token is not None
        assert abs(token.expires_at - token.granted_at - DEFAULT_TTL_SECONDS) < 2

    def test_jit_grant_unique_tokens(self, manager):
        """多次 grant 生成不同 token"""
        t1 = manager.grant(tool_name="t1")
        t2 = manager.grant(tool_name="t2")
        assert t1.token != t2.token


# =====================================================================
# 2. verify 有效 token
# =====================================================================


class TestJitVerifyValidToken:
    def test_jit_verify_valid_token(self, manager):
        """有效 token 验证通过"""
        token = manager.grant(tool_name="write_file", scope="write")
        assert manager.verify(token.token, "write_file", "write") is True

    def test_jit_verify_without_scope(self, manager):
        """scope=None 时不校验 scope"""
        token = manager.grant(tool_name="write_file", scope="write")
        assert manager.verify(token.token, "write_file") is True
        assert manager.verify(token.token, "write_file", scope=None) is True


# =====================================================================
# 3. 过期 token 验证失败
# =====================================================================


class TestJitVerifyExpiredTokenFails:
    def test_jit_verify_expired_token_fails(self, manager):
        """过期 token 验证失败"""
        token = manager.grant(tool_name="write_file", scope="write", ttl_seconds=1)
        # 等待过期
        time.sleep(1.1)
        assert manager.verify(token.token, "write_file", "write") is False

    def test_jit_token_is_expired_method(self):
        """JITToken.is_expired 正确判断"""
        now = time.time()
        t = JITToken(
            token="abc",
            tool_name="t",
            scope="s",
            granted_at=now - 10,
            expires_at=now - 1,
            granted_to="",
        )
        assert t.is_expired() is True
        t2 = JITToken(
            token="abc",
            tool_name="t",
            scope="s",
            granted_at=now,
            expires_at=now + 100,
            granted_to="",
        )
        assert t2.is_expired() is False


# =====================================================================
# 4. 撤销 token
# =====================================================================


class TestJitRevokeInvalidates:
    def test_jit_revoke_invalidates(self, manager):
        """撤销后 token 验证失败"""
        token = manager.grant(tool_name="write_file", scope="write")
        assert manager.verify(token.token, "write_file", "write") is True
        assert manager.revoke(token.token) is True
        assert manager.verify(token.token, "write_file", "write") is False

    def test_jit_revoke_nonexistent_returns_false(self, manager):
        """撤销不存在的 token 返回 False"""
        assert manager.revoke("nonexistent-token") is False


# =====================================================================
# 5. 清理过期 token
# =====================================================================


class TestJitCleanupExpired:
    def test_jit_cleanup_expired(self, manager):
        """cleanup_expired 清理所有过期 token"""
        # 发放 3 个短 TTL token + 1 个长 TTL token
        t1 = manager.grant(tool_name="t1", ttl_seconds=1)
        t2 = manager.grant(tool_name="t2", ttl_seconds=1)
        t3 = manager.grant(tool_name="t3", ttl_seconds=1)
        t4 = manager.grant(tool_name="t4", ttl_seconds=3600)
        assert manager.count() == 4

        # 等待前 3 个过期
        time.sleep(1.1)
        removed = manager.cleanup_expired()
        assert removed == 3
        assert manager.count() == 1
        # t4 仍然有效
        assert manager.verify(t4.token, "t4") is True
        # 前 3 个已失效
        assert manager.verify(t1.token, "t1") is False
        assert manager.verify(t2.token, "t2") is False
        assert manager.verify(t3.token, "t3") is False

    def test_jit_cleanup_no_expired_returns_zero(self, manager):
        """没有过期 token 时 cleanup 返回 0"""
        manager.grant(tool_name="t1", ttl_seconds=3600)
        manager.grant(tool_name="t2", ttl_seconds=3600)
        assert manager.cleanup_expired() == 0
        assert manager.count() == 2


# =====================================================================
# 6. feature flag 关闭
# =====================================================================


class TestJitDisabledNoop:
    def test_jit_disabled_grant_returns_none(self, monkeypatch, tmp_jit_path):
        """feature flag 关闭：grant 返回 None"""
        monkeypatch.setattr(jit_module, "JIT_PERMISSION_ENABLED", False)
        m = JITPermissionManager(persist_path=tmp_jit_path)
        token = m.grant(tool_name="write_file", scope="write")
        assert token is None
        # 文件不创建
        assert not tmp_jit_path.exists()

    def test_jit_disabled_verify_returns_true(self, monkeypatch, tmp_jit_path):
        """feature flag 关闭：verify 返回 True（兼容旧路径，不阻断）"""
        monkeypatch.setattr(jit_module, "JIT_PERMISSION_ENABLED", False)
        m = JITPermissionManager(persist_path=tmp_jit_path)
        # 任何 token 都返回 True（向后兼容）
        assert m.verify("any-token", "any-tool") is True
        assert m.verify("any-token", "any-tool", "any-scope") is True
        assert m.verify("", "any-tool") is True

    def test_jit_disabled_revoke_returns_false(self, monkeypatch, tmp_jit_path):
        """feature flag 关闭：revoke 返回 False"""
        monkeypatch.setattr(jit_module, "JIT_PERMISSION_ENABLED", False)
        m = JITPermissionManager(persist_path=tmp_jit_path)
        assert m.revoke("any-token") is False

    def test_jit_disabled_cleanup_returns_zero(self, monkeypatch, tmp_jit_path):
        """feature flag 关闭：cleanup_expired 返回 0"""
        monkeypatch.setattr(jit_module, "JIT_PERMISSION_ENABLED", False)
        m = JITPermissionManager(persist_path=tmp_jit_path)
        assert m.cleanup_expired() == 0

    def test_jit_disabled_count_returns_zero(self, monkeypatch, tmp_jit_path):
        """feature flag 关闭：count 返回 0"""
        monkeypatch.setattr(jit_module, "JIT_PERMISSION_ENABLED", False)
        m = JITPermissionManager(persist_path=tmp_jit_path)
        assert m.count() == 0


# =====================================================================
# 7. 工具名/scope 不匹配
# =====================================================================


class TestJitVerifyMismatch:
    def test_jit_verify_wrong_tool_fails(self, manager):
        """工具名不匹配验证失败"""
        token = manager.grant(tool_name="write_file", scope="write")
        assert manager.verify(token.token, "delete_file", "write") is False
        assert manager.verify(token.token, "write_file", "write") is True

    def test_jit_verify_wrong_scope_fails(self, manager):
        """scope 不匹配验证失败"""
        token = manager.grant(tool_name="write_file", scope="write")
        assert manager.verify(token.token, "write_file", "delete") is False
        # scope=None 不校验
        assert manager.verify(token.token, "write_file", None) is True

    def test_jit_verify_nonexistent_token_fails(self, manager):
        """不存在的 token 验证失败"""
        assert manager.verify("nonexistent", "any-tool") is False

    def test_jit_verify_empty_token_fails(self, manager):
        """空 token 验证失败"""
        assert manager.verify("", "any-tool") is False


# =====================================================================
# 8. 持久化往返
# =====================================================================


class TestJitPersistence:
    def test_jit_persistence_roundtrip(self, tmp_jit_path):
        """grant 后重启能加载 token，verify 仍通过"""
        m1 = JITPermissionManager(persist_path=tmp_jit_path)
        token = m1.grant(tool_name="write_file", scope="write", ttl_seconds=3600)
        assert tmp_jit_path.exists()

        # 重新构造 manager（模拟重启）
        m2 = JITPermissionManager(persist_path=tmp_jit_path)
        assert m2.verify(token.token, "write_file", "write") is True
        assert m2.count() == 1

    def test_jit_persistence_corrupt_file(self, tmp_jit_path):
        """持久化文件损坏时不抛异常，加载为空"""
        tmp_jit_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_jit_path.write_text("not a valid json {", encoding="utf-8")
        # 不抛异常
        m = JITPermissionManager(persist_path=tmp_jit_path)
        assert m.count() == 0

    def test_jit_revoke_persisted(self, tmp_jit_path):
        """revoke 后持久化，重启后 token 仍无效"""
        m1 = JITPermissionManager(persist_path=tmp_jit_path)
        token = m1.grant(tool_name="write_file", scope="write", ttl_seconds=3600)
        m1.revoke(token.token)

        m2 = JITPermissionManager(persist_path=tmp_jit_path)
        assert m2.verify(token.token, "write_file", "write") is False
        assert m2.count() == 0


# =====================================================================
# 9. 全局单例
# =====================================================================


class TestJitGlobalSingleton:
    def test_get_jit_manager_singleton(self):
        """get_jit_manager 返回同一实例"""
        m1 = get_jit_manager()
        m2 = get_jit_manager()
        assert m1 is m2

    def test_reset_jit_manager(self):
        """reset 后下次 get 返回新实例"""
        m1 = get_jit_manager()
        reset_jit_manager()
        m2 = get_jit_manager()
        assert m1 is not m2


# =====================================================================
# 10. JITToken 序列化
# =====================================================================


class TestJitTokenSerialization:
    def test_to_dict_from_dict_roundtrip(self):
        """to_dict / from_dict 往返"""
        t = JITToken(
            token="abc123",
            tool_name="write_file",
            scope="write",
            granted_at=1000.0,
            expires_at=1300.0,
            granted_to="alice",
        )
        d = t.to_dict()
        t2 = JITToken.from_dict(d)
        assert t2.token == t.token
        assert t2.tool_name == t.tool_name
        assert t2.scope == t.scope
        assert t2.granted_at == t.granted_at
        assert t2.expires_at == t.expires_at
        assert t2.granted_to == t.granted_to

    def test_from_dict_missing_fields_uses_defaults(self):
        """from_dict 缺失字段填默认"""
        t = JITToken.from_dict({"token": "abc"})
        assert t.token == "abc"
        assert t.tool_name == ""
        assert t.scope == ""
        assert t.granted_at == 0.0
        assert t.expires_at == 0.0
        assert t.granted_to == ""
