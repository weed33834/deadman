"""D19:边缘推理硬件安全(Edge Inference Hardware Security)。

问题:
    deadman `alignment/local_llm.py` 仅是 OpenAI 兼容客户端,
    无任何硬件 / 模型安全防护:
        - 无模型签名校验(模型可被替换 / 后门)
        - 无 TEE / SGX / 安全飞地(推理过程可被观测)
        - 无 secure boot / measured boot(运行环境不可信)
        - 凭证(api_key)明文存储在配置文件
        - 模型权重可被篡改(供应链攻击)
        - 推理日志可泄漏 PII

    商业场景:
        - 企业私有部署 deadman + 本地 LLM(数据不出境)
        - 但若本地 LLM 被篡改 → 推理结果被操控 → 误导用户决策
        - 凭证泄漏 → 攻击者冒充用户调用 API

缓解:
    - ModelSignatureVerifier:模型签名校验(SHA-256 + 可选 GPG)
    - TEEAbstraction:TEE / SGX / TrustZone 抽象接口(占位)
    - SecureBootValidator:启动环境校验(占位)
    - CredentialProtector:本地凭证加密存储(用 TPM / OS keychain)
    - InferenceAuditor:推理审计(记录模型版本 / 输入输出 hash / 时间戳)

设计:
    verifier = ModelSignatureVerifier()
    # 1. 注册模型 + 签名
    verifier.register("llama-7b", expected_hash="sha256:abc...")
    # 2. 加载前校验
    if not verifier.verify("llama-7b", model_path="/models/llama.gguf"):
        raise SecurityError("Model signature mismatch")
    # 3. 加载模型
    client.load_model("llama-7b")
    # 4. 推理后审计
    auditor.log_inference(model="llama-7b", input_hash=..., output_hash=...)

注意:
    - 本模块为"接口抽象",生产环境需对接实际硬件(TPM / SGX / TrustZone)
    - 模型签名校验是基于文件 hash 的简化版,生产应使用数字签名(RSA / ECDSA)

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


class VerificationStatus(str, Enum):
    """模型签名校验状态。"""

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    NOT_REGISTERED = "not_registered"
    FILE_NOT_FOUND = "file_not_found"
    DISABLED = "disabled"


@dataclass
class ModelSignature:
    """模型签名档案。"""

    model_name: str
    expected_hash: str  # sha256:hex
    signature: str = ""  # 可选 GPG / RSA 签名
    registered_at: float = field(default_factory=time.time)
    registered_by: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """校验结果。"""

    model_name: str
    status: VerificationStatus
    actual_hash: str = ""
    expected_hash: str = ""
    file_path: str = ""
    file_size: int = 0
    verified_at: float = field(default_factory=time.time)
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class InferenceAuditRecord:
    """推理审计记录。"""

    timestamp: float
    model_name: str
    model_version: str = ""
    input_hash: str = ""  # 输入内容 SHA-256(不存原文)
    output_hash: str = ""  # 输出内容 SHA-256
    input_token_count: int = 0
    output_token_count: int = 0
    duration_ms: int = 0
    user_id: str = ""  # 可脱敏后存
    tenant_id: str = ""
    # 安全状态
    signature_verified: bool = True
    tee_enabled: bool = False
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class ModelSignatureVerifier:
    """模型签名校验器。

    用法:
        verifier = ModelSignatureVerifier()
        # 1. 注册模型 + 期望签名
        verifier.register(
            model_name="llama-7b",
            expected_hash="sha256:abc123...",
            registered_by="ops-team",
        )
        # 2. 加载前校验
        result = verifier.verify("llama-7b", "/models/llama.gguf")
        if result.status != VerificationStatus.VERIFIED:
            raise SecurityError(f"Model verification failed: {result.status}")
        # 3. 加载模型
        client.load_model("llama-7b")
    """

    def __init__(self, store_path: Optional[str] = None) -> None:
        self.store_path = store_path
        self._lock = threading.RLock()
        # model_name -> ModelSignature
        self._signatures: dict[str, ModelSignature] = {}
        # 校验历史(最近 N 次)
        self._verification_history: list[VerificationResult] = []
        if store_path and os.path.exists(store_path):
            self._load()

    def register(
        self,
        model_name: str,
        expected_hash: str,
        *,
        signature: str = "",
        registered_by: str = "",
        metadata: Optional[dict] = None,
    ) -> ModelSignature:
        """注册模型签名。

        Args:
            model_name: 模型名
            expected_hash: 期望的文件 hash(format: "sha256:hex")
            signature: 可选数字签名(GPG / RSA / ECDSA)
            registered_by: 注册者(用于审计)
            metadata: 自定义元数据(版本 / 来源 / ...)
        """
        sig = ModelSignature(
            model_name=model_name,
            expected_hash=expected_hash,
            signature=signature,
            registered_by=registered_by,
            metadata=metadata or {},
        )
        with self._lock:
            self._signatures[model_name] = sig
            self._save()
        logger.info(
            "Registered model signature: %s (hash=%s...)",
            model_name, expected_hash[:20],
        )
        return sig

    def verify(
        self,
        model_name: str,
        model_path: str,
    ) -> VerificationResult:
        """校验模型文件签名。

        Args:
            model_name: 已注册的模型名
            model_path: 模型文件路径

        Returns:
            VerificationResult: VERIFIED / MISMATCH / NOT_REGISTERED / FILE_NOT_FOUND
        """
        result = VerificationResult(
            model_name=model_name,
            status=VerificationStatus.VERIFIED,
            file_path=model_path,
        )

        if not is_enabled("defense"):
            result.status = VerificationStatus.DISABLED
            return result

        with self._lock:
            sig = self._signatures.get(model_name)
            if sig is None:
                result.status = VerificationStatus.NOT_REGISTERED
                result.error = f"Model '{model_name}' not registered"
                self._verification_history.append(result)
                return result

        # 文件存在性
        if not os.path.exists(model_path):
            result.status = VerificationStatus.FILE_NOT_FOUND
            result.error = f"Model file not found: {model_path}"
            with self._lock:
                self._verification_history.append(result)
            return result

        # 计算 hash
        try:
            actual_hash = self._compute_file_hash(model_path)
            result.actual_hash = actual_hash
            result.expected_hash = sig.expected_hash
            result.file_size = os.path.getsize(model_path)
        except Exception as e:
            result.status = VerificationStatus.MISMATCH
            result.error = f"Hash computation failed: {e}"
            with self._lock:
                self._verification_history.append(result)
            return result

        if actual_hash != sig.expected_hash:
            result.status = VerificationStatus.MISMATCH
            result.error = (
                f"Hash mismatch: expected {sig.expected_hash[:20]}..., "
                f"got {actual_hash[:20]}..."
            )
            logger.error(
                "Model signature mismatch for %s at %s (expected=%s, actual=%s)",
                model_name, model_path,
                sig.expected_hash[:20], actual_hash[:20],
            )
        else:
            logger.info("Model %s verified (hash=%s...)", model_name, actual_hash[:20])

        with self._lock:
            self._verification_history.append(result)
            # 限制历史长度
            if len(self._verification_history) > 10_000:
                self._verification_history = self._verification_history[-5_000:]
        return result

    def list_models(self) -> list[ModelSignature]:
        with self._lock:
            return list(self._signatures.values())

    def get_history(self, limit: int = 100) -> list[VerificationResult]:
        with self._lock:
            return list(self._verification_history[-limit:])

    def unregister(self, model_name: str) -> bool:
        with self._lock:
            existed = model_name in self._signatures
            self._signatures.pop(model_name, None)
            if existed:
                self._save()
            return existed

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _compute_file_hash(file_path: str, chunk_size: int = 8192) -> str:
        """计算文件 SHA-256。"""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
            data = {
                "signatures": {
                    k: asdict(v) for k, v in self._signatures.items()
                },
            }
            tmp = self.store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Failed to save signature store: %s", e)

    def _load(self) -> None:
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("signatures", {}).items():
                self._signatures[k] = ModelSignature(
                    model_name=v["model_name"],
                    expected_hash=v["expected_hash"],
                    signature=v.get("signature", ""),
                    registered_at=v.get("registered_at", time.time()),
                    registered_by=v.get("registered_by", ""),
                    metadata=v.get("metadata", {}),
                )
        except Exception as e:
            logger.error("Failed to load signature store: %s", e)


class TEEAbstraction:
    """可信执行环境(TEE)抽象接口。

    本类为占位实现,生产环境应针对具体硬件:
        - Intel SGX:使用 Open Enclave SDK / Gramine
        - AMD SEV:使用 SEV-SNP API
        - ARM TrustZone:使用 OP-TEE
        - Apple Secure Enclave:使用 CryptoKit

    接口约定:
        - is_available():TEE 是否可用
        - attest():获取远程证明(quote)
        - secure_compute(func, *args):在 TEE 中执行
        - seal_data(data):用 TEE 密钥加密数据
        - unseal_data(encrypted):解密数据
    """

    def __init__(self, backend: str = "auto") -> None:
        """初始化 TEE 抽象。

        Args:
            backend: "auto" / "sgx" / "sev" / "trustzone" / "none"
        """
        self.backend = backend
        self._available = self._detect_tee()

    def is_available(self) -> bool:
        """TEE 是否可用。"""
        return self._available

    def get_backend(self) -> str:
        """当前使用的 TEE 后端。"""
        if not self._available:
            return "none"
        return self.backend

    def attest(self) -> dict:
        """获取远程证明(quote)。

        返回的 quote 可发送给远程验证方,验证运行环境可信。
        """
        if not self._available:
            return {"available": False, "reason": "TEE not available"}
        # 占位:实际返回硬件 quote
        return {
            "available": True,
            "backend": self.backend,
            "quote": "PLACEHOLDER_QUOTE",
            "timestamp": time.time(),
            "mr_enclave": "PLACEHOLDER_HASH",  # MRENCLAVE
            "mr_signer": "PLACEHOLDER_HASH",  # MRSIGNER
        }

    def secure_compute(self, func: callable, *args, **kwargs) -> Any:
        """在 TEE 中执行函数。

        生产实现:
            - SGX:将 func 序列化,在 enclave 中加载并执行
            - SEV:在加密 VM 中执行
        """
        if not self._available:
            # 降级到普通执行(生产环境应拒绝)
            logger.warning("TEE not available, executing in plain mode (insecure)")
            return func(*args, **kwargs)
        # 占位:实际应封装到 TEE 中执行
        return func(*args, **kwargs)

    def seal_data(self, data: bytes) -> bytes:
        """用 TEE 密钥加密数据(仅当前 TEE 可解密)。"""
        if not self._available:
            # 降级到普通 hash(非加密,仅完整性)
            return data
        # 占位:实际用 TEE 密钥加密
        return data

    def unseal_data(self, encrypted: bytes) -> bytes:
        """解密 TEE 加密的数据。"""
        if not self._available:
            return encrypted
        return encrypted

    # ==================================================================
    # 内部
    # ==================================================================

    def _detect_tee(self) -> bool:
        """检测 TEE 硬件是否可用。"""
        if self.backend == "none":
            return False
        if self.backend != "auto":
            return True  # 强制声明可用

        # 自动检测(简化版)
        # SGX:检查 /dev/isgx 或 /dev/sgx_enclave
        if os.path.exists("/dev/isgx") or os.path.exists("/dev/sgx_enclave"):
            self.backend = "sgx"
            return True
        # SEV:检查 /dev/sev
        if os.path.exists("/dev/sev"):
            self.backend = "sev"
            return True
        # TrustZone:不易从用户态检测
        return False


class InferenceAuditor:
    """推理审计器。

    用法:
        auditor = InferenceAuditor(store_path=".traecli/data/inference_audit.jsonl")
        # 推理前
        input_hash = auditor.hash_content(user_input)
        # 推理后
        output_hash = auditor.hash_content(output)
        auditor.log_inference(
            model_name="llama-7b",
            input_hash=input_hash,
            output_hash=output_hash,
            duration_ms=1234,
            user_id="u1",
        )
    """

    def __init__(self, store_path: Optional[str] = None) -> None:
        self.store_path = store_path
        self._lock = threading.RLock()
        self._records: list[InferenceAuditRecord] = []

    def log_inference(self, record: InferenceAuditRecord) -> None:
        """记录推理审计。"""
        with self._lock:
            self._records.append(record)
            if len(self._records) > 10_000:
                self._records = self._records[-5_000:]
        # 持久化(JSONL 格式)
        if self.store_path:
            try:
                os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
                with open(self.store_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error("Failed to write inference audit: %s", e)

    def list_records(
        self,
        model_name: Optional[str] = None,
        limit: int = 100,
    ) -> list[InferenceAuditRecord]:
        with self._lock:
            records = list(self._records)
        if model_name:
            records = [r for r in records if r.model_name == model_name]
        return records[-limit:]

    @staticmethod
    def hash_content(content: str) -> str:
        """计算内容 hash(用于审计,不存原文)。"""
        if not content:
            return ""
        return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]}"

    def detect_anomalies(self) -> list[dict]:
        """检测推理异常(简化版)。

        检查:
            - 同一输入产生不同输出(模型不一致)
            - 推理时间异常(可能 DoS / 资源耗尽)
            - 输入 token 数异常(可能 prompt injection)
        """
        anomalies = []
        with self._lock:
            records = list(self._records[-1000:])

        # 1. 同输入不同输出
        input_groups: dict[str, list[InferenceAuditRecord]] = {}
        for r in records:
            if r.input_hash:
                input_groups.setdefault(r.input_hash, []).append(r)
        for input_hash, group in input_groups.items():
            outputs = {r.output_hash for r in group if r.output_hash}
            if len(outputs) > 1:
                anomalies.append({
                    "type": "output_inconsistency",
                    "input_hash": input_hash[:20],
                    "distinct_outputs": len(outputs),
                    "occurrences": len(group),
                })

        # 2. 推理时间异常(> 3x 平均)
        if records:
            durations = [r.duration_ms for r in records if r.duration_ms > 0]
            if durations:
                avg = sum(durations) / len(durations)
                for r in records:
                    if r.duration_ms > avg * 3 and avg > 0:
                        anomalies.append({
                            "type": "slow_inference",
                            "model": r.model_name,
                            "duration_ms": r.duration_ms,
                            "average_ms": int(avg),
                        })

        # 3. 输入 token 异常
        for r in records:
            if r.input_token_count > 10_000:
                anomalies.append({
                    "type": "large_input",
                    "model": r.model_name,
                    "input_tokens": r.input_token_count,
                })

        return anomalies


# =====================================================================
# 全局单例
# =====================================================================

_verifier: Optional[ModelSignatureVerifier] = None
_tee: Optional[TEEAbstraction] = None
_auditor: Optional[InferenceAuditor] = None
_lock = threading.Lock()


def get_model_signature_verifier(
    store_path: Optional[str] = None,
) -> ModelSignatureVerifier:
    global _verifier
    with _lock:
        if _verifier is None:
            path = store_path or os.environ.get(
                "DEADMAN_MODEL_SIGNATURE_STORE",
                ".traecli/data/model_signatures.json",
            )
            _verifier = ModelSignatureVerifier(store_path=path)
        return _verifier


def get_tee_abstraction(backend: str = "auto") -> TEEAbstraction:
    global _tee
    with _lock:
        if _tee is None:
            _tee = TEEAbstraction(backend=backend)
        return _tee


def get_inference_auditor(
    store_path: Optional[str] = None,
) -> InferenceAuditor:
    global _auditor
    with _lock:
        if _auditor is None:
            path = store_path or os.environ.get(
                "DEADMAN_INFERENCE_AUDIT_PATH",
                ".traecli/data/inference_audit.jsonl",
            )
            _auditor = InferenceAuditor(store_path=path)
        return _auditor


def reset_edge_security_singletons() -> None:
    """重置所有单例(测试用)。"""
    global _verifier, _tee, _auditor
    with _lock:
        _verifier = None
        _tee = None
        _auditor = None
