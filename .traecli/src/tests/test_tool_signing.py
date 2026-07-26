"""P3.5 测试矩阵 - 工具签名校验（供应链安全）

覆盖：
  1. sign + verify 一致性（有 cryptography 时用真实签名，否则降级 hash 校验）
  2. 篡改 manifest 应被拒
  3. cryptography 不可用时降级到 schema_hash 校验
  4. feature flag 关闭时一律放行

通过 monkeypatch 控制 feature flag。
"""

from __future__ import annotations

import pytest

from deadman.mcp_server import signing as signing_module
from deadman.mcp_server.signing import (
    REGISTERED_MANIFESTS,
    ToolManifest,
    build_manifest,
    compute_schema_hash,
    register_manifest,
    sign_manifest,
    verify_manifest,
    verify_tool_integrity,
)


# =====================================================================
# 辅助：生成测试用 Ed25519 密钥对（若 cryptography 可用）
# =====================================================================


def _gen_ed25519_keypair():
    """返回 (private_pem, public_pem)；cryptography 不可用时返回 (None, None)"""
    if not signing_module._CRYPTOGRAPHY_AVAILABLE:
        return None, None
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv_pem, pub_pem


# =====================================================================
# fixture
# =====================================================================


@pytest.fixture
def signing_enabled(monkeypatch):
    """临时打开 TOOL_SIGNING_ENABLED"""
    monkeypatch.setattr(signing_module, "TOOL_SIGNING_ENABLED", True)
    # 清空注册表
    REGISTERED_MANIFESTS.clear()
    yield
    REGISTERED_MANIFESTS.clear()


@pytest.fixture
def signing_disabled(monkeypatch):
    """显式关闭 TOOL_SIGNING_ENABLED"""
    monkeypatch.setattr(signing_module, "TOOL_SIGNING_ENABLED", False)
    yield


@pytest.fixture
def patched_public_key(monkeypatch):
    """注入测试公钥（若有 cryptography）"""
    _, pub_pem = _gen_ed25519_keypair()
    if pub_pem:
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", pub_pem)
    yield pub_pem


@pytest.fixture
def patched_keypair(monkeypatch):
    """注入测试密钥对（若有 cryptography）"""
    priv_pem, pub_pem = _gen_ed25519_keypair()
    if priv_pem:
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PRIVATE_KEY_PEM", priv_pem)
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", pub_pem)
    yield (priv_pem, pub_pem)


# =====================================================================
# schema_hash 计算
# =====================================================================


class TestSchemaHash:
    def test_schema_hash_stable(self):
        """相同 schema（不同字段顺序）应产生相同 hash"""
        h1 = compute_schema_hash({"a": 1, "b": 2})
        h2 = compute_schema_hash({"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 64

    def test_schema_hash_different(self):
        """不同 schema 应产生不同 hash"""
        h1 = compute_schema_hash({"a": 1})
        h2 = compute_schema_hash({"a": 2})
        assert h1 != h2


# =====================================================================
# 签名 / 验签
# =====================================================================


class TestSignAndVerify:
    def test_sign_and_verify_manifest(self, signing_enabled, patched_keypair):
        """签名后验签应通过"""
        priv_pem, _ = patched_keypair
        if not priv_pem:
            pytest.skip("cryptography 不可用，跳过真实签名测试")

        manifest = build_manifest(
            name="test_tool",
            version="1.0.0",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            private_key_pem=priv_pem,
        )
        assert manifest.signature != ""
        assert verify_manifest(manifest) is True

    def test_sign_returns_empty_without_cryptography(self, signing_enabled, monkeypatch):
        """cryptography 不可用时 sign_manifest 应返回空字符串（降级）"""
        monkeypatch.setattr(signing_module, "_CRYPTOGRAPHY_AVAILABLE", False)
        manifest = ToolManifest(
            name="x", version="1", schema_hash="0" * 64, signature=""
        )
        sig = sign_manifest(manifest, private_key_pem="dummy")
        assert sig == ""

    def test_verify_without_public_key_falls_back_to_hash(self, signing_enabled, monkeypatch):
        """公钥未配置时降级到 schema_hash 校验"""
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", "")
        # schema_hash 合法（64 hex）→ 通过
        manifest = ToolManifest(
            name="x", version="1", schema_hash="a" * 64, signature=""
        )
        assert verify_manifest(manifest) is True
        # schema_hash 长度不对 → 拒绝
        bad = ToolManifest(
            name="x", version="1", schema_hash="short", signature=""
        )
        assert verify_manifest(bad) is False


# =====================================================================
# 篡改检测
# =====================================================================


class TestTamperDetection:
    def test_tampered_manifest_rejected(self, signing_enabled, patched_keypair):
        """篡改 manifest 字段后验签应失败"""
        priv_pem, _ = patched_keypair
        if not priv_pem:
            pytest.skip("cryptography 不可用")

        manifest = build_manifest(
            name="test_tool",
            version="1.0.0",
            input_schema={"type": "object"},
            private_key_pem=priv_pem,
        )
        # 验证原始可通过
        assert verify_manifest(manifest) is True
        # 篡改 name
        tampered = ToolManifest(
            name="tampered_name",
            version=manifest.version,
            schema_hash=manifest.schema_hash,
            signature=manifest.signature,
        )
        assert verify_manifest(tampered) is False

    def test_tampered_schema_hash_rejected(self, signing_enabled, patched_keypair):
        """篡改 schema_hash 后验签应失败"""
        priv_pem, _ = patched_keypair
        if not priv_pem:
            pytest.skip("cryptography 不可用")

        manifest = build_manifest(
            name="t",
            version="1",
            input_schema={"type": "object"},
            private_key_pem=priv_pem,
        )
        tampered = ToolManifest(
            name=manifest.name,
            version=manifest.version,
            schema_hash="b" * 64,  # 篡改 hash
            signature=manifest.signature,
        )
        assert verify_manifest(tampered) is False

    def test_missing_signature_rejected_when_pubkey_set(
        self, signing_enabled, patched_public_key
    ):
        """公钥已配置但 signature 为空应拒绝"""
        _, pub_pem = patched_public_key, None
        if not signing_module.TOOL_SIGNING_PUBLIC_KEY_PEM:
            pytest.skip("cryptography 不可用")
        manifest = ToolManifest(
            name="x", version="1", schema_hash="a" * 64, signature=""
        )
        assert verify_manifest(manifest) is False

    def test_invalid_signature_hex_rejected(self, signing_enabled, patched_public_key):
        """非 hex 字符串的 signature 应被拒"""
        if not signing_module.TOOL_SIGNING_PUBLIC_KEY_PEM:
            pytest.skip("cryptography 不可用")
        manifest = ToolManifest(
            name="x",
            version="1",
            schema_hash="a" * 64,
            signature="not-hex!@#$",
        )
        assert verify_manifest(manifest) is False


# =====================================================================
# 降级路径
# =====================================================================


class TestSigningFallback:
    def test_signing_unavailable_falls_back_to_hash(
        self, signing_enabled, monkeypatch
    ):
        """cryptography 不可用时降级到 schema_hash 校验"""
        monkeypatch.setattr(signing_module, "_CRYPTOGRAPHY_AVAILABLE", False)
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", "")
        # schema_hash 合法 → 通过
        manifest = ToolManifest(
            name="x", version="1", schema_hash="a" * 64, signature=""
        )
        assert verify_manifest(manifest) is True

    def test_register_manifest_fallback(self, signing_enabled, monkeypatch):
        """降级模式下 register_manifest 应成功（仅 hash 校验）"""
        monkeypatch.setattr(signing_module, "_CRYPTOGRAPHY_AVAILABLE", False)
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", "")
        manifest = ToolManifest(
            name="fallback_tool",
            version="1.0.0",
            schema_hash=compute_schema_hash({"type": "object"}),
            signature="",
        )
        assert register_manifest(manifest) is True
        assert "fallback_tool" in REGISTERED_MANIFESTS


# =====================================================================
# verify_tool_integrity
# =====================================================================


class TestVerifyToolIntegrity:
    def test_integrity_passes_for_unregistered(self, signing_enabled):
        """未注册 manifest 的工具应放行"""
        assert verify_tool_integrity("unregistered", {"type": "object"}) is True

    def test_integrity_passes_when_hash_matches(self, signing_enabled, monkeypatch):
        """schema_hash 一致时应通过"""
        monkeypatch.setattr(signing_module, "_CRYPTOGRAPHY_AVAILABLE", False)
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", "")
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        manifest = ToolManifest(
            name="t",
            version="1",
            schema_hash=compute_schema_hash(schema),
            signature="",
        )
        register_manifest(manifest)
        assert verify_tool_integrity("t", schema) is True

    def test_integrity_fails_when_hash_differs(self, signing_enabled, monkeypatch):
        """schema_hash 不一致时应拒绝（疑似篡改）"""
        monkeypatch.setattr(signing_module, "_CRYPTOGRAPHY_AVAILABLE", False)
        monkeypatch.setattr(signing_module, "TOOL_SIGNING_PUBLIC_KEY_PEM", "")
        original_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        tampered_schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        manifest = ToolManifest(
            name="t",
            version="1",
            schema_hash=compute_schema_hash(original_schema),
            signature="",
        )
        register_manifest(manifest)
        assert verify_tool_integrity("t", tampered_schema) is False


# =====================================================================
# feature flag 关闭时一律放行
# =====================================================================


class TestSigningDisabled:
    def test_signing_disabled_passthrough(self, signing_disabled):
        """feature flag 关闭时 verify_manifest 一律 True"""
        manifest = ToolManifest(
            name="x", version="1", schema_hash="whatever", signature=""
        )
        assert verify_manifest(manifest) is True

    def test_signing_disabled_integrity_passthrough(self, signing_disabled):
        """feature flag 关闭时 verify_tool_integrity 一律 True"""
        assert verify_tool_integrity("any", {"any": "schema"}) is True

    def test_signing_disabled_tampered_still_passes(self, signing_disabled):
        """feature flag 关闭时即使 manifest 被篡改也放行（保证旧行为不变）"""
        manifest = ToolManifest(
            name="x", version="1", schema_hash="tampered", signature="bad"
        )
        assert verify_manifest(manifest) is True
