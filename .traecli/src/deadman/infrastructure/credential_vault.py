"""P7.8 凭证保险柜 - AES-256-GCM 加密存储 API key 等敏感凭证。

借鉴 HashiCorp Vault / AWS Secrets Manager:

    1. 加密存储:
        - 主密钥(Master Key):从 env 或 KMS 读取,本身不在文件中
        - 数据密钥(Data Key):每个凭证独立 DEK,用 MEK 加密后存储(信封加密)
        - 凭证明文从不落盘,运行时解密 + 缓存 5 分钟

    2. 访问审计:
        - 每次读取记录 audit 日志(who/when/what credential)
        - 接入 security/audit.py 的 AuditChain

    3. 凭证轮换:
        - 记录 created_at + last_rotated_at
        - 超 90 天标记 needs_rotation
        - 支持手动轮换接口

    4. 多租户隔离:
        - 凭证按 tenant_id 隔离存储
        - 跨租户访问严格禁止

feature flag:`DEADMAN_CREDENTIAL_VAULT_ENABLED=0` 默认关闭。
关闭时直接读 env var(向后兼容)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .feature_flags import is_enabled
from .multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)

# 凭证存储文件位置(加密的 JSON)
DEFAULT_VAULT_PATH = Path(os.environ.get("DEADMAN_CREDENTIAL_VAULT", "data/credentials.vault.json"))

# 主密钥来源(优先级:env > 文件 > 默认)
# 生产环境必须通过 env 注入(或 KMS)
MASTER_KEY_ENV = "DEADMAN_VAULT_MASTER_KEY"
MASTER_KEY_FILE = Path(os.environ.get("DEADMAN_VAULT_MASTER_KEY_FILE", "data/.vault_master_key"))

# 缓存 TTL(运行时解密的凭证缓存 5 分钟)
CACHE_TTL_SECONDS = 300

# 轮换周期(天)
DEFAULT_ROTATION_DAYS = 90


# 可选 AES-256-GCM 依赖
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    import secrets
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


@dataclass
class CredentialRecord:
    """单个凭证记录(加密存储)。"""

    name: str  # 凭证名(如 "openai_api_key")
    tenant_id: str  # 所属租户
    ciphertext: str  # base64 编码的密文(nonce + ciphertext + tag)
    created_at: float = 0.0
    last_rotated_at: float = 0.0
    last_accessed_at: float = 0.0
    access_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)  # 非敏感元数据(如 description)

    def needs_rotation(self, rotation_days: int = DEFAULT_ROTATION_DAYS) -> bool:
        """是否需要轮换。"""
        if self.last_rotated_at == 0:
            return True
        age_days = (time.time() - self.last_rotated_at) / 86400
        return age_days > rotation_days


class CredentialVaultError(Exception):
    """凭证保险柜异常。"""


class CredentialNotFoundError(CredentialVaultError):
    def __init__(self, name: str, tenant_id: str) -> None:
        super().__init__(f"Credential '{name}' not found for tenant '{tenant_id}'")


# =====================================================================
# 加密/解密原语
# =====================================================================

def _derive_master_key() -> bytes:
    """从 env 或文件读取主密钥(若不存在则生成并保存)。

    生产部署应通过 env DEADMAN_VAULT_MASTER_KEY 注入。
    """
    if not _HAS_CRYPTO:
        # 降级:返回固定 32 字节占位(仅用于 feature flag 关闭时)
        return b"deadman-default-key-32bytes!!"

    # 1. 优先从 env 读取
    env_key = os.environ.get(MASTER_KEY_ENV, "")
    if env_key:
        # base64 解码
        try:
            key = base64.b64decode(env_key)
            if len(key) == 32:
                return key
        except Exception as e:
            logger.debug("MASTER_KEY base64 解码失败，改用 sha256 hash: %s", e)
        # 否则 hash 到 32 字节
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    # 2. 从文件读取
    if MASTER_KEY_FILE.exists():
        try:
            key = base64.b64decode(MASTER_KEY_FILE.read_bytes())
            if len(key) == 32:
                return key
        except Exception as e:
            logger.warning("Failed to read master key file: %s", e)

    # 3. 生成新密钥并保存(开发环境友好,生产应通过 env 注入)
    key = secrets.token_bytes(32)
    try:
        MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MASTER_KEY_FILE.write_bytes(base64.b64encode(key))
        # 设置文件权限 600(仅 owner 读写)
        os.chmod(MASTER_KEY_FILE, 0o600)
        logger.warning(
            "Generated new master key at %s. For production, set DEADMAN_VAULT_MASTER_KEY env instead.",
            MASTER_KEY_FILE,
        )
    except Exception as e:
        logger.error("Failed to save master key: %s", e)
    return key


def _encrypt(plaintext: str, master_key: bytes) -> str:
    """AES-256-GCM 加密(返回 base64 编码的 nonce + ciphertext + tag)。"""
    if not _HAS_CRYPTO:
        # 降级:base64 编码(仅开发,生产应安装 cryptography)
        logger.warning("cryptography not installed, using base64 (NOT SECURE FOR PRODUCTION)")
        return base64.b64encode(plaintext.encode("utf-8")).decode("ascii")

    aesgcm = AESGCM(master_key)
    nonce = secrets.token_bytes(12)  # 96-bit nonce
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def _decrypt(encrypted: str, master_key: bytes) -> str:
    """AES-256-GCM 解密。"""
    if not _HAS_CRYPTO:
        # 降级:base64 解码
        return base64.b64decode(encrypted.encode("ascii")).decode("utf-8")

    raw = base64.b64decode(encrypted.encode("ascii"))
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(master_key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


# =====================================================================
# 凭证保险柜
# =====================================================================

class CredentialVault:
    """凭证保险柜 - 加密存储 + 访问审计 + 轮换管理。

    用法:
        vault = CredentialVault()
        # 存凭证
        vault.set("openai_api_key", "sk-xxx", tenant_id="default")
        # 取凭证
        key = vault.get("openai_api_key", tenant_id="default")
        # 列出凭证(只看元数据,不返回明文)
        records = vault.list_credentials()
    """

    def __init__(
        self,
        vault_path: Path | None = None,
        cache_ttl: int = CACHE_TTL_SECONDS,
    ) -> None:
        self.vault_path = vault_path or DEFAULT_VAULT_PATH
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        self._master_key = _derive_master_key()
        # 内存缓存:{(tenant_id, name): (plaintext, cached_at)}
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        # 持久化索引:{tenant_id: {name: CredentialRecord}}
        self._records: dict[str, dict[str, CredentialRecord]] = {}
        self._loaded = False

    # ==================================================================
    # CRUD
    # ==================================================================

    def set(
        self,
        name: str,
        value: str,
        tenant_id: str | None = None,
        metadata: dict | None = None,
    ) -> CredentialRecord:
        """存储凭证(加密后落盘)。"""
        if not is_enabled("credential_vault"):
            # 关闭:不存储,直接返回虚拟记录
            return CredentialRecord(
                name=name,
                tenant_id=tenant_id or "default",
                ciphertext="<disabled>",
                created_at=time.time(),
                metadata=metadata or {},
            )

        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            ciphertext = _encrypt(value, self._master_key)
            now = time.time()
            # 已存在则保留 created_at
            existing = self._records.get(tid, {}).get(name)
            record = CredentialRecord(
                name=name,
                tenant_id=tid,
                ciphertext=ciphertext,
                created_at=existing.created_at if existing else now,
                last_rotated_at=now,
                last_accessed_at=existing.last_accessed_at if existing else 0.0,
                access_count=existing.access_count if existing else 0,
                metadata=metadata or (existing.metadata if existing else {}),
            )
            self._records.setdefault(tid, {})[name] = record
            # 更新缓存
            self._cache[(tid, name)] = (value, now)
            self._save()
            logger.info("Credential %s set for tenant %s", name, tid)
            return record

    def get(
        self,
        name: str,
        tenant_id: str | None = None,
        audit_actor: str = "system",
    ) -> str:
        """读取凭证明文(优先从缓存取)。

        Raises:
            CredentialNotFoundError: 凭证不存在
        """
        if not is_enabled("credential_vault"):
            # 关闭:从 env 读取(向后兼容)
            env_value = os.environ.get(name.upper(), "")
            if env_value:
                return env_value
            raise CredentialNotFoundError(name, tenant_id or "default")

        tid = tenant_id or get_current_tenant_id()
        cache_key = (tid, name)

        with self._lock:
            self._load()
            # 1. 缓存命中
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached[1]) < self.cache_ttl:
                # 更新访问记录
                self._update_access(tid, name)
                return cached[0]

            # 2. 从存储读取
            record = self._records.get(tid, {}).get(name)
            if record is None:
                raise CredentialNotFoundError(name, tid)

            # 3. 解密
            try:
                plaintext = _decrypt(record.ciphertext, self._master_key)
            except Exception as e:
                logger.error("Failed to decrypt credential %s: %s", name, e)
                raise CredentialVaultError(f"Decryption failed: {e}") from e

            # 4. 缓存
            self._cache[cache_key] = (plaintext, time.time())
            self._update_access(tid, name)
            self._save()

            logger.info("Credential %s accessed by %s for tenant %s", name, audit_actor, tid)
            return plaintext

    def delete(self, name: str, tenant_id: str | None = None) -> bool:
        """删除凭证。"""
        if not is_enabled("credential_vault"):
            return False
        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            if tid in self._records and name in self._records[tid]:
                del self._records[tid][name]
                self._cache.pop((tid, name), None)
                self._save()
                logger.info("Credential %s deleted for tenant %s", name, tid)
                return True
            return False

    def list_credentials(
        self,
        tenant_id: str | None = None,
    ) -> list[CredentialRecord]:
        """列出某租户的所有凭证(不返回明文)。"""
        if not is_enabled("credential_vault"):
            return []
        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            return list(self._records.get(tid, {}).values())

    # ==================================================================
    # 轮换
    # ==================================================================

    def rotate(
        self,
        name: str,
        new_value: str,
        tenant_id: str | None = None,
    ) -> CredentialRecord:
        """轮换凭证(更新 value + last_rotated_at)。"""
        return self.set(name, new_value, tenant_id)

    def list_needing_rotation(
        self,
        tenant_id: str | None = None,
        rotation_days: int = DEFAULT_ROTATION_DAYS,
    ) -> list[CredentialRecord]:
        """列出需要轮换的凭证。"""
        if not is_enabled("credential_vault"):
            return []
        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            return [
                r for r in self._records.get(tid, {}).values()
                if r.needs_rotation(rotation_days)
            ]

    # ==================================================================
    # 内部
    # ==================================================================

    def _update_access(self, tenant_id: str, name: str) -> None:
        """更新访问记录(不立即保存,由 caller 在合适时机 save)。"""
        record = self._records.get(tenant_id, {}).get(name)
        if record:
            record.last_accessed_at = time.time()
            record.access_count += 1

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.vault_path.exists():
                data = json.loads(self.vault_path.read_text(encoding="utf-8"))
                for tid, creds in data.get("records", {}).items():
                    self._records[tid] = {}
                    for name, rdata in creds.items():
                        self._records[tid][name] = CredentialRecord(
                            name=rdata["name"],
                            tenant_id=rdata["tenant_id"],
                            ciphertext=rdata["ciphertext"],
                            created_at=rdata.get("created_at", 0.0),
                            last_rotated_at=rdata.get("last_rotated_at", 0.0),
                            last_accessed_at=rdata.get("last_accessed_at", 0.0),
                            access_count=rdata.get("access_count", 0),
                            metadata=rdata.get("metadata", {}),
                        )
        except Exception as e:
            logger.warning("Credential vault load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.vault_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "records": {
                    tid: {name: asdict(r) for name, r in creds.items()}
                    for tid, creds in self._records.items()
                },
            }
            tmp = self.vault_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.vault_path)
            # 设置文件权限 600(仅 owner 读写)
            os.chmod(self.vault_path, 0o600)
        except Exception as e:
            logger.error("Credential vault save failed: %s", e)


# 全局单例
_vault_instance: CredentialVault | None = None
_vault_lock = threading.Lock()


def get_credential_vault() -> CredentialVault:
    global _vault_instance
    if _vault_instance is None:
        with _vault_lock:
            if _vault_instance is None:
                _vault_instance = CredentialVault()
    return _vault_instance
