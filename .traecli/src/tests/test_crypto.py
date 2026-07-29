"""utils/crypto.py 专项测试 - AES-256-GCM 加密原语

测试覆盖：
    1. derive_key: PBKDF2 密钥派生
    2. encrypt_envelope / decrypt_envelope: JSON 可序列化加密
    3. encrypt_bytes / decrypt_bytes: 二进制 blob 加密
    4. v1/v2/v3 向后兼容解密
    5. 错误处理：空口令、篡改密文、截断密文
    6. 随机性：相同明文每次加密结果不同
    7. 往返一致性：encrypt → decrypt 还原原文
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

import pytest

from deadman.utils.crypto import (
    _decrypt_v1,
    _decrypt_v2,
    _v2_derive_subkey,
    _v2_keystream,
    decrypt_bytes,
    decrypt_envelope,
    derive_key,
    encrypt_bytes,
    encrypt_envelope,
)


# =====================================================================
# 1. derive_key 测试
# =====================================================================


class TestDeriveKey:
    def test_basic_derivation(self):
        """PBKDF2 派生应返回 32 字节密钥"""
        key = derive_key(b"my-passphrase", b"my-salt")
        assert len(key) == 32

    def test_deterministic(self):
        """相同输入应产生相同输出"""
        key1 = derive_key(b"pass", b"salt")
        key2 = derive_key(b"pass", b"salt")
        assert key1 == key2

    def test_different_passphrase_different_key(self):
        """不同口令应产生不同密钥"""
        key1 = derive_key(b"pass1", b"salt")
        key2 = derive_key(b"pass2", b"salt")
        assert key1 != key2

    def test_different_salt_different_key(self):
        """不同盐应产生不同密钥"""
        key1 = derive_key(b"pass", b"salt1")
        key2 = derive_key(b"pass", b"salt2")
        assert key1 != key2

    def test_custom_iterations(self):
        """自定义迭代次数应影响输出"""
        key1 = derive_key(b"pass", b"salt", iterations=100_000)
        key2 = derive_key(b"pass", b"salt", iterations=200_000)
        assert key1 != key2


# =====================================================================
# 2. encrypt_envelope / decrypt_envelope 测试
# =====================================================================


class TestEnvelopeEncryption:
    def test_roundtrip_text(self):
        """文本加密 → 解密往返一致"""
        plaintext = b"Hello, World!"
        passphrase = b"secret-key"
        envelope = encrypt_envelope(plaintext, passphrase)
        recovered = decrypt_envelope(envelope, passphrase)
        assert recovered == plaintext

    def test_roundtrip_json(self):
        """JSON 数据加密 → 解密往返一致"""
        plaintext = b'{"name": "\\u5f20\\u4e09", "age": 30, "items": [1, 2, 3]}'
        passphrase = b"json-secret"
        envelope = encrypt_envelope(plaintext, passphrase)
        recovered = decrypt_envelope(envelope, passphrase)
        assert recovered == plaintext

    def test_roundtrip_large_data(self):
        """大数据（1MB）加密 → 解密往返一致"""
        plaintext = secrets.token_bytes(1024 * 1024)
        passphrase = b"large-data-key"
        envelope = encrypt_envelope(plaintext, passphrase)
        recovered = decrypt_envelope(envelope, passphrase)
        assert recovered == plaintext

    def test_envelope_format(self):
        """envelope 格式应包含正确字段"""
        envelope = encrypt_envelope(b"test", b"pass")
        assert "nonce" in envelope
        assert "salt" in envelope
        assert "ct" in envelope
        assert envelope["alg"] == "aes-256-gcm"
        assert envelope["version"] == 3

    def test_nonce_is_random(self):
        """每次加密 nonce 应不同"""
        env1 = encrypt_envelope(b"test", b"pass")
        env2 = encrypt_envelope(b"test", b"pass")
        assert env1["nonce"] != env2["nonce"]

    def test_salt_is_random(self):
        """每次加密 salt 应不同"""
        env1 = encrypt_envelope(b"test", b"pass")
        env2 = encrypt_envelope(b"test", b"pass")
        assert env1["salt"] != env2["salt"]

    def test_ciphertext_not_plaintext(self):
        """密文不应包含明文"""
        plaintext = b"sensitive-data-12345"
        envelope = encrypt_envelope(plaintext, b"pass")
        ct_raw = base64.b64decode(envelope["ct"])
        assert plaintext not in ct_raw

    def test_empty_plaintext(self):
        """空明文加密 → 解密应正常"""
        envelope = encrypt_envelope(b"", b"pass")
        recovered = decrypt_envelope(envelope, b"pass")
        assert recovered == b""

    def test_empty_passphrase_raises(self):
        """空口令应抛出 ValueError"""
        with pytest.raises(ValueError, match="加密口令为空"):
            encrypt_envelope(b"test", b"")

    def test_wrong_passphrase_raises(self):
        """错误口令解密应抛出 ValueError"""
        envelope = encrypt_envelope(b"secret", b"correct-pass")
        with pytest.raises(ValueError, match="AES-GCM 解密失败"):
            decrypt_envelope(envelope, b"wrong-pass")

    def test_tampered_ciphertext_raises(self):
        """篡改密文应抛出 ValueError（GCM 认证失败）"""
        envelope = encrypt_envelope(b"secret", b"pass")
        # 篡改密文
        ct_bytes = bytearray(base64.b64decode(envelope["ct"]))
        ct_bytes[0] ^= 0xFF
        envelope["ct"] = base64.b64encode(bytes(ct_bytes)).decode("ascii")
        with pytest.raises(ValueError, match="AES-GCM 解密失败"):
            decrypt_envelope(envelope, b"pass")

    def test_tampered_nonce_raises(self):
        """篡改 nonce 应抛出 ValueError"""
        envelope = encrypt_envelope(b"secret", b"pass")
        nonce_bytes = bytearray(base64.b64decode(envelope["nonce"]))
        nonce_bytes[0] ^= 0xFF
        envelope["nonce"] = base64.b64encode(bytes(nonce_bytes)).decode("ascii")
        with pytest.raises(ValueError, match="AES-GCM 解密失败"):
            decrypt_envelope(envelope, b"pass")


# =====================================================================
# 3. encrypt_bytes / decrypt_bytes 测试
# =====================================================================


class TestBytesEncryption:
    def test_roundtrip(self):
        """二进制加密 → 解密往返一致"""
        plaintext = b"binary-data-\x00\x01\x02"
        key = derive_key(b"pass", b"salt")
        ct = encrypt_bytes(plaintext, key)
        recovered = decrypt_bytes(ct, key)
        assert recovered == plaintext

    def test_ciphertext_format(self):
        """密文格式：nonce(12) || ciphertext + tag"""
        plaintext = b"test"
        key = derive_key(b"pass", b"salt")
        ct = encrypt_bytes(plaintext, key)
        # nonce(12) + ciphertext(len(plaintext)) + tag(16)
        assert len(ct) == 12 + len(plaintext) + 16

    def test_nonce_is_random(self):
        """每次加密 nonce 不同"""
        key = derive_key(b"pass", b"salt")
        ct1 = encrypt_bytes(b"test", key)
        ct2 = encrypt_bytes(b"test", key)
        assert ct1[:12] != ct2[:12]

    def test_wrong_key_raises(self):
        """错误密钥解密应抛异常"""
        plaintext = b"secret"
        key1 = derive_key(b"pass1", b"salt")
        key2 = derive_key(b"pass2", b"salt")
        ct = encrypt_bytes(plaintext, key1)
        with pytest.raises(Exception):
            decrypt_bytes(ct, key2)

    def test_short_ciphertext_raises(self):
        """过短密文应抛出 ValueError"""
        with pytest.raises(ValueError, match="密文过短"):
            decrypt_bytes(b"short", b"key-32-bytes-xxxxxxxxxxxxxxxx")

    def test_empty_plaintext(self):
        """空明文加密 → 解密"""
        key = derive_key(b"pass", b"salt")
        ct = encrypt_bytes(b"", key)
        recovered = decrypt_bytes(ct, key)
        assert recovered == b""


# =====================================================================
# 4. 向后兼容测试（v1/v2 解密）
# =====================================================================


class TestBackwardCompatibility:
    def test_decrypt_v3(self):
        """v3 envelope 可被 decrypt_envelope 正确解密"""
        plaintext = b"v3-data"
        passphrase = b"v3-pass"
        envelope = encrypt_envelope(plaintext, passphrase)
        assert envelope["version"] == 3
        recovered = decrypt_envelope(envelope, passphrase)
        assert recovered == plaintext

    def test_decrypt_v2_compatibility(self):
        """v2 envelope（HMAC keystream）可被 _decrypt_v2 解密"""
        passphrase = b"v2-pass"
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        plaintext = b"v2-legacy-data"

        # 构造 v2 envelope
        enc_key = _v2_derive_subkey(passphrase, salt, b"enc")
        mac_key = _v2_derive_subkey(passphrase, salt, b"mac")
        keystream = _v2_keystream(enc_key, len(plaintext), nonce)
        ct = bytes(a ^ b for a, b in zip(plaintext, keystream))
        tag = hmac.new(mac_key, ct, hashlib.sha256).digest()

        v2_envelope = {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "salt": base64.b64encode(salt).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
            "version": 2,
        }

        # decrypt_envelope 应自动路由到 _decrypt_v2
        recovered = decrypt_envelope(v2_envelope, passphrase)
        assert recovered == plaintext

    def test_decrypt_v2_tampered_tag_raises(self):
        """v2 篡改 tag 应抛出 ValueError"""
        passphrase = b"v2-pass"
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        plaintext = b"v2-data"

        enc_key = _v2_derive_subkey(passphrase, salt, b"enc")
        mac_key = _v2_derive_subkey(passphrase, salt, b"mac")
        keystream = _v2_keystream(enc_key, len(plaintext), nonce)
        ct = bytes(a ^ b for a, b in zip(plaintext, keystream))
        tag = hmac.new(mac_key, ct, hashlib.sha256).digest()
        # 篡改 tag
        tag = bytes([tag[0] ^ 0xFF]) + tag[1:]

        v2_envelope = {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "salt": base64.b64encode(salt).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
            "version": 2,
        }
        with pytest.raises(ValueError, match="HMAC tag 校验失败"):
            decrypt_envelope(v2_envelope, passphrase)

    def test_decrypt_v1_compatibility(self):
        """v1 envelope（无口令）可被 _decrypt_v1 解密"""
        # v1 使用固定密钥
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        plaintext = b"v1-legacy-data"

        enc_key = hashlib.pbkdf2_hmac("sha256", b"enc", salt, 1000, dklen=32)
        keystream = _v2_keystream(enc_key, len(plaintext), nonce)
        ct = bytes(a ^ b for a, b in zip(plaintext, keystream))

        v1_envelope = {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "salt": base64.b64encode(salt).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }

        recovered = _decrypt_v1(v1_envelope)
        assert recovered == plaintext

    def test_decrypt_envelope_routes_by_version(self):
        """decrypt_envelope 应根据 version 字段路由到正确的解密器"""
        # v3
        env_v3 = encrypt_envelope(b"test", b"pass")
        assert env_v3["version"] == 3
        assert decrypt_envelope(env_v3, b"pass") == b"test"

        # v1（无 version 字段默认为 1）
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        plaintext = b"v1-data"
        enc_key = hashlib.pbkdf2_hmac("sha256", b"enc", salt, 1000, dklen=32)
        keystream = _v2_keystream(enc_key, len(plaintext), nonce)
        ct = bytes(a ^ b for a, b in zip(plaintext, keystream))
        env_v1 = {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "salt": base64.b64encode(salt).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }
        assert decrypt_envelope(env_v1, b"any-pass") == b"v1-data"


# =====================================================================
# 5. 安全性测试
# =====================================================================


class TestSecurity:
    def test_no_plaintext_in_envelope(self):
        """envelope 中不应包含明文（base64 编码后也不应出现）"""
        plaintext = b"super-secret-data-1234567890"
        envelope = encrypt_envelope(plaintext, b"pass")
        envelope_str = str(envelope)
        assert "super-secret" not in envelope_str

    def test_no_key_in_envelope(self):
        """envelope 中不应包含派生密钥"""
        passphrase = b"my-secret-passphrase"
        key = derive_key(passphrase, b"some-salt")
        envelope = encrypt_envelope(b"data", passphrase)
        assert base64.b64encode(key).decode("ascii") not in str(envelope)

    def test_different_passphrase_different_ciphertext(self):
        """不同口令对相同明文应产生不同密文"""
        plaintext = b"same-plaintext"
        env1 = encrypt_envelope(plaintext, b"pass1")
        env2 = encrypt_envelope(plaintext, b"pass2")
        assert env1["ct"] != env2["ct"]

    def test_passphrase_not_in_envelope(self):
        """口令不应出现在 envelope 中"""
        passphrase = b"unique-passphrase-xyz"
        envelope = encrypt_envelope(b"data", passphrase)
        envelope_str = str(envelope)
        assert "unique-passphrase" not in envelope_str
        assert passphrase.decode("ascii") not in envelope_str
