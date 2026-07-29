"""P3.5 工具签名校验（供应链安全）

每个工具声明一个 ToolManifest，含：
  - name        : 工具名
  - version     : 版本号
  - schema_hash : input_schema 的 SHA-256 哈希（防篡改）
  - signature   : 用私钥对 (name|version|schema_hash) 的 RSA/Ed25519 签名（hex）

签名流程：
  1. 发布者本地用私钥签 manifest，得到 signature
  2. manifest 与 signature 一起分发给部署方
  3. 部署方在工具注册时（或启动时）用公钥验签 + 比对 schema_hash
  4. 任一不匹配 → 拒绝注册 / 拒绝调用

降级路径：
  - cryptography 不可用 → 仅做 schema_hash 校验（无签名验证），不阻断
  - 公钥未配置 → 仅做 schema_hash 校验
  - 验签抛异常 → 视为未通过

Feature flag:DEADMAN_TOOL_SIGNING_ENABLED=0（默认关闭）
关闭时 verify_manifest 一律返回 True，注册表纯本地维护。

注意：本模块不依赖 server.py，避免循环 import。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

# =====================================================================
# 配置（feature flag，默认关闭）
# =====================================================================

TOOL_SIGNING_ENABLED: bool = os.environ.get(
    "DEADMAN_TOOL_SIGNING_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 可选：预置的公钥（PEM 格式字符串），用于验签
# 未配置时仅做 schema_hash 校验
TOOL_SIGNING_PUBLIC_KEY_PEM: str = os.environ.get(
    "DEADMAN_TOOL_SIGNING_PUBLIC_KEY_PEM", ""
)

# 可选：预置的私钥（PEM 格式字符串），用于签名（仅发布方需要）
TOOL_SIGNING_PRIVATE_KEY_PEM: str = os.environ.get(
    "DEADMAN_TOOL_SIGNING_PRIVATE_KEY_PEM", ""
)

# cryptography 可选依赖
try:
    from cryptography.hazmat.primitives import hashes, serialization  # type: ignore
    from cryptography.hazmat.primitives.asymmetric import (
        ed25519,
        padding,
        rsa,
    )
    from cryptography.exceptions import InvalidSignature  # type: ignore

    _CRYPTOGRAPHY_AVAILABLE = True
except Exception:  # pragma: no cover - 环境降级
    _CRYPTOGRAPHY_AVAILABLE = False
    InvalidSignature = Exception  # type: ignore[misc,assignment]


# =====================================================================
# ToolManifest
# =====================================================================


@dataclass
class ToolManifest:
    """工具 manifest（供应链元数据）"""

    name: str
    version: str
    schema_hash: str
    signature: str = ""

    def to_signing_payload(self) -> bytes:
        """构造签名输入：name|version|schema_hash"""
        return f"{self.name}|{self.version}|{self.schema_hash}".encode()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =====================================================================
# 哈希计算
# =====================================================================


def compute_schema_hash(input_schema: dict[str, Any]) -> str:
    """计算 input_schema 的 SHA-256 哈希

    sort_keys=True 保证字段顺序无关；default=str 兜底不可序列化对象。
    """
    payload = json.dumps(
        input_schema or {}, sort_keys=True, default=str, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# =====================================================================
# 签名 / 验签
# =====================================================================


def _load_private_key(pem: str):
    """从 PEM 加载私钥（RSA 或 Ed25519）"""
    if not _CRYPTOGRAPHY_AVAILABLE:
        return None
    if not pem:
        return None
    try:
        return serialization.load_pem_private_key(
            pem.encode("utf-8"), password=None
        )
    except Exception:
        return None


def _load_public_key(pem: str):
    """从 PEM 加载公钥（RSA 或 Ed25519）"""
    if not _CRYPTOGRAPHY_AVAILABLE:
        return None
    if not pem:
        return None
    try:
        return serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception:
        return None


def sign_manifest(manifest: ToolManifest, private_key_pem: str | None = None) -> str:
    """用私钥对 manifest 签名，返回 hex 签名

    优先用入参 private_key_pem；否则回退到环境变量 TOOL_SIGNING_PRIVATE_KEY_PEM。

    降级：
      - cryptography 不可用 → 返回空字符串（仅做 hash 校验）
      - 私钥不可用 → 返回空字符串
      - 签名失败 → 返回空字符串
    """
    if not _CRYPTOGRAPHY_AVAILABLE:
        return ""

    pem = private_key_pem or TOOL_SIGNING_PRIVATE_KEY_PEM
    if not pem:
        return ""

    key = _load_private_key(pem)
    if key is None:
        return ""

    payload = manifest.to_signing_payload()
    try:
        if isinstance(key, ed25519.Ed25519PrivateKey):
            sig = key.sign(payload)
        elif isinstance(key, rsa.RSAPrivateKey):
            sig = key.sign(
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            # 其他类型（如 ECDSA）：尽力签名
            try:
                sig = key.sign(payload)  # type: ignore[call-arg]
            except Exception:
                return ""
        return sig.hex()
    except Exception:
        return ""


def verify_manifest(
    manifest: ToolManifest, public_key_pem: str | None = None
) -> bool:
    """校验 manifest 签名

    返回 True 表示通过：
      - signature 非空且验签成功
      - signature 为空（无签名）：仅当 cryptography 不可用或公钥未配置时降级放行

    返回 False 表示验签失败：
      - signature 非空但验签失败（篡改 / 错误密钥）
      - 公钥已配置但 signature 为空（缺失签名）

    feature flag 关闭时一律返回 True（保证旧行为不变）。
    """
    if not TOOL_SIGNING_ENABLED:
        return True

    # 公钥未配置或 cryptography 不可用：仅做 schema_hash 一致性校验
    pem = public_key_pem or TOOL_SIGNING_PUBLIC_KEY_PEM
    if not pem or not _CRYPTOGRAPHY_AVAILABLE:
        # 降级：只要 schema_hash 非空（64 hex）即视为通过
        return bool(manifest.schema_hash) and len(manifest.schema_hash) == 64

    pub = _load_public_key(pem)
    if pub is None:
        # 公钥加载失败：降级到 hash 校验
        return bool(manifest.schema_hash) and len(manifest.schema_hash) == 64

    # 公钥已配置但 manifest 缺签名：拒绝
    if not manifest.signature:
        return False

    payload = manifest.to_signing_payload()
    try:
        sig_bytes = bytes.fromhex(manifest.signature)
    except ValueError:
        return False

    try:
        if isinstance(pub, ed25519.Ed25519PublicKey):
            pub.verify(sig_bytes, payload)
        elif isinstance(pub, rsa.RSAPublicKey):
            pub.verify(
                sig_bytes,
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            try:
                pub.verify(sig_bytes, payload)  # type: ignore[call-arg]
            except Exception:
                return False
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


# =====================================================================
# 已注册 manifest 表
# =====================================================================

REGISTERED_MANIFESTS: dict[str, ToolManifest] = field(default_factory=dict)  # type: ignore[assignment]
# 注：上面 field(default_factory=dict) 在模块级是错的，下方覆盖为真实 dict
REGISTERED_MANIFESTS = {}  # type: ignore[assignment]


def register_manifest(manifest: ToolManifest, public_key_pem: str | None = None) -> bool:
    """注册一个工具 manifest

    返回 True 表示注册成功（验签通过或处于降级模式）。
    返回 False 表示验签失败，拒绝注册。
    注册成功后写入 REGISTERED_MANIFESTS。
    """
    if not verify_manifest(manifest, public_key_pem):
        return False
    REGISTERED_MANIFESTS[manifest.name] = manifest
    return True


def unregister_manifest(name: str) -> bool:
    """注销 manifest，返回是否删除成功"""
    return REGISTERED_MANIFESTS.pop(name, None) is not None


def get_manifest(name: str) -> ToolManifest | None:
    return REGISTERED_MANIFESTS.get(name)


def verify_tool_integrity(
    name: str, input_schema: dict[str, Any], public_key_pem: str | None = None
) -> bool:
    """校验运行中的工具 schema 是否与已注册 manifest 一致

    返回 True 表示：
      - 该工具未注册 manifest（动态注册的新工具，未纳入签名体系）→ 放行
      - manifest 已注册且 schema_hash 一致 → 通过
      - feature flag 关闭 → 一律放行

    返回 False 表示：manifest 已注册但 schema_hash 不一致（疑似篡改）
    """
    if not TOOL_SIGNING_ENABLED:
        return True

    manifest = REGISTERED_MANIFESTS.get(name)
    if manifest is None:
        # 未注册 manifest：放行（不强制所有工具都注册）
        return True

    actual_hash = compute_schema_hash(input_schema)
    if actual_hash != manifest.schema_hash:
        return False

    # 顺带验一次签名（防 manifest 自身被篡改）
    return verify_manifest(manifest, public_key_pem)


def build_manifest(
    name: str,
    version: str,
    input_schema: dict[str, Any],
    private_key_pem: str | None = None,
) -> ToolManifest:
    """便捷工厂：根据 schema 计算哈希并签名"""
    schema_hash = compute_schema_hash(input_schema)
    manifest = ToolManifest(
        name=name, version=version, schema_hash=schema_hash, signature=""
    )
    manifest.signature = sign_manifest(manifest, private_key_pem)
    return manifest
