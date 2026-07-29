"""共享加密原语 - AES-256-GCM + PBKDF2 密钥派生

统一 ending_note / vault / deadman_switch 等模块的加密实现，
消除各模块各自手写流密码的冗余（R1/W1/W2 修复）。

设计：
  - 密钥派生：PBKDF2-HMAC-SHA256（100k 迭代，32 字节输出）
  - 对称加密：AES-256-GCM（认证加密，nonce + ciphertext + tag 一体）
  - 两种输出格式：
    * envelope（JSON 可序列化 dict）— 用于 ending_note 等需 JSON 存储的场景
    * bytes（二进制 blob）— 用于 vault 等直接文件存储的场景
  - 向后兼容：可解密 v1（无口令）/ v2（HMAC keystream）旧 envelope

依赖：cryptography>=41.0（已在 pyproject.toml 声明）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KDF_ITERATIONS = 100_000
_KEY_LEN = 32  # 256-bit
_NONCE_LEN = 12  # 96-bit (AES-GCM standard)
_SALT_LEN = 16  # 128-bit

# 旧版 v2 加密参数（向后兼容解密用）
_V2_KDF_ITERATIONS = 100_000
_V2_HMAC_ALGO = hashlib.sha256


def derive_key(passphrase: bytes, salt: bytes, iterations: int = _KDF_ITERATIONS) -> bytes:
    """PBKDF2-HMAC-SHA256 密钥派生

    Args:
        passphrase: 用户口令字节
        salt: 随机盐
        iterations: PBKDF2 迭代次数（默认 100k）

    Returns:
        32 字节派生密钥（用于 AES-256-GCM）
    """
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, iterations, dklen=_KEY_LEN)


# =====================================================================
# envelope 格式（JSON 可序列化）— 用于 ending_note
# =====================================================================

def encrypt_envelope(plaintext: bytes, passphrase: bytes) -> dict[str, str]:
    """AES-256-GCM 加密，返回 JSON 可序列化的 envelope

    Args:
        plaintext: 明文字节
        passphrase: 用户口令字节（不可为空）

    Returns:
        envelope dict:
          - nonce:  base64(12 字节随机 nonce)
          - salt:   base64(16 字节随机盐)
          - ct:     base64(密文 + GCM tag)
          - alg:    "aes-256-gcm"
          - version: 3
    """
    if not passphrase:
        raise ValueError("加密口令为空：必须传入 user_passphrase")

    nonce = secrets.token_bytes(_NONCE_LEN)
    salt = secrets.token_bytes(_SALT_LEN)
    key = derive_key(passphrase, salt)

    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, None)

    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "salt": base64.b64encode(salt).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "alg": "aes-256-gcm",
        "version": 3,
    }


def decrypt_envelope(envelope: dict[str, Any], passphrase: bytes) -> bytes:
    """解密 envelope（支持 v1/v2/v3 向后兼容）

    Args:
        envelope: encrypt_envelope 或旧版 _encrypt 返回的 envelope dict
        passphrase: 用户口令字节

    Raises:
        ValueError: 口令为空或 tag 校验失败
    """
    if not passphrase:
        raise ValueError("解密口令为空")

    version = envelope.get("version", 1)
    alg = envelope.get("alg", "")

    if version >= 3 or alg == "aes-256-gcm":
        return _decrypt_v3(envelope, passphrase)
    elif version == 2:
        return _decrypt_v2(envelope, passphrase)
    else:
        return _decrypt_v1(envelope)


def _decrypt_v3(envelope: dict[str, Any], passphrase: bytes) -> bytes:
    """v3: AES-256-GCM 解密"""
    nonce = base64.b64decode(envelope["nonce"])
    salt = base64.b64decode(envelope["salt"])
    ct = base64.b64decode(envelope["ct"])

    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ct, None)
    except Exception as exc:
        # AES-GCM 校验失败（篡改 / 密钥不匹配）统一抛 ValueError，
        # 与 v2 的 HMAC tag 校验失败行为一致，上层 except ValueError 可捕获
        raise ValueError(f"AES-GCM 解密失败：{exc}") from exc


def _decrypt_v2(envelope: dict[str, Any], passphrase: bytes) -> bytes:
    """v2 向后兼容：HMAC-SHA256 keystream 解密（旧 ending_note 数据迁移用）"""
    nonce = base64.b64decode(envelope["nonce"])
    salt = base64.b64decode(envelope["salt"])
    ct = base64.b64decode(envelope["ct"])
    tag = base64.b64decode(envelope["tag"])

    enc_key = _v2_derive_subkey(passphrase, salt, b"enc")
    mac_key = _v2_derive_subkey(passphrase, salt, b"mac")

    expected_tag = hmac.new(mac_key, ct, _V2_HMAC_ALGO).digest()
    if not hmac.compare_digest(expected_tag, tag):
        raise ValueError("HMAC tag 校验失败：文件已被篡改或密钥不匹配")

    keystream = _v2_keystream(enc_key, len(ct), nonce)
    return bytes(a ^ b for a, b in zip(ct, keystream, strict=True))


def _decrypt_v1(envelope: dict[str, Any]) -> bytes:
    """v1 向后兼容：无口令解密（Phase 14 之前的 envelope，仅用于读取旧数据迁移）"""
    nonce = base64.b64decode(envelope["nonce"])
    salt = base64.b64decode(envelope["salt"])
    ct = base64.b64decode(envelope["ct"])

    enc_key = hashlib.pbkdf2_hmac("sha256", b"enc", salt, 1000, dklen=32)
    keystream = _v2_keystream(enc_key, len(ct), nonce)
    return bytes(a ^ b for a, b in zip(ct, keystream, strict=True))


# --- v2 兼容内部函数 ---

def _v2_derive_subkey(passphrase: bytes, salt: bytes, info: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", info + b":" + passphrase, salt, _V2_KDF_ITERATIONS, dklen=_KEY_LEN
    )


def _v2_keystream(key: bytes, length: int, nonce: bytes) -> bytes:
    """v2 兼容：HMAC-SHA256 counter-mode keystream"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), _V2_HMAC_ALGO).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


# =====================================================================
# bytes 格式（二进制 blob）— 用于 vault
# =====================================================================

def encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    """AES-256-GCM 加密为二进制 blob

    格式：nonce(12) || ciphertext + tag

    Args:
        plaintext: 明文字节
        key: 32 字节密钥（由 derive_key 派生）

    Returns:
        二进制密文 blob
    """
    nonce = secrets.token_bytes(_NONCE_LEN)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ct


def decrypt_bytes(ciphertext: bytes, key: bytes) -> bytes:
    """AES-256-GCM 解密二进制 blob

    Args:
        ciphertext: encrypt_bytes 返回的二进制 blob
        key: 32 字节密钥

    Returns:
        明文字节；校验失败抛 ValueError
    """
    if len(ciphertext) < _NONCE_LEN + 16:
        raise ValueError("密文过短")
    nonce = ciphertext[:_NONCE_LEN]
    ct = ciphertext[_NONCE_LEN:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None)
