"""D10:凭证保险柜主密钥备份(防主密钥丢失致业务停摆)。

问题(v1.4 联动风险:凭证保险柜主密钥丢失):
    主密钥(MEK)丢失则所有凭证不可解密,业务完全停摆。
    不可恢复。

    威胁场景:
        1. 主密钥文件被误删 / 损坏
        2. KMS 服务下线 / 不可访问
        3. 主密钥被恶意删除(攻击者)
        4. 硬件故障(磁盘损坏)
        5. 自然灾害(机房损坏)

缓解(Shamir Secret Sharing - SSS):
    将主密钥分成 N 份(N=5),任意 K 份(K=3)可重建。
    分发到不同地理位置 / 不同保管人 / 不同存储介质。

    1. 主密钥生成时自动 SSS 分片
    2. 分片分发到多个保管人(可加密邮件 / 物理介质)
    3. 主密钥丢失时,K 个保管人汇集分片 → 重建
    4. 重建后立即轮换主密钥(旧分片作废)
    5. 定期演练(每季度)验证分片可重建

设计:
    - KeyShare: 单个分片
    - MasterKeyBackup: 备份管理器(分片 / 重建 / 演练)
    - DrillRecord: 演练记录(审计)

集成:
    credential_vault.py 初始化主密钥时:
        backup = get_master_key_backup()
        if not backup.has_backup():
            shares = backup.split(master_key, n=5, k=3)
            backup.distribute(shares, recipients=[...])

    credential_vault.py 加载主密钥失败时:
        backup = get_master_key_backup()
        if backup.has_backup():
            shares = backup.collect_shares(k=3)
            master_key = backup.reconstruct(shares)

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..feature_flags import is_enabled

logger = logging.getLogger(__name__)

# 默认分片参数(N=5 分片,K=3 重建)
DEFAULT_N_SHARES = 5
DEFAULT_K_THRESHOLD = 3

# 默认备份存储路径
DEFAULT_BACKUP_PATH = Path(
    os.environ.get("DEADMAN_MASTER_KEY_BACKUP", "data/defense/master_key_backup")
)


class BackupStatus(str, Enum):
    """备份状态。"""

    NOT_INITIALIZED = "not_initialized"  # 未初始化(无主密钥)
    BACKED_UP = "backed_up"  # 已备份(分片已分发)
    RECOVERED = "recovered"  # 已恢复(主密钥曾丢失,通过分片重建)
    ROTATED = "rotated"  # 已轮换(分片作废,重新分发)
    DRILL_VERIFIED = "drill_verified"  # 演练验证通过


@dataclass
class KeyShare:
    """主密钥分片(SSS)。

    注意:分片本身不可单独重建主密钥,需 K 个分片汇集。
    """

    share_id: str  # 分片 ID(0..N-1)
    share_index: int  # SSS 索引(x 坐标)
    share_value: str  # 分片值(y 坐标,hex)
    recipient: str = ""  # 保管人标识(邮箱 / 部门)
    distributed_at: float = 0.0  # 分发时间
    received_back_at: float | None = None  # 回收时间(用于重建)


@dataclass
class DrillRecord:
    """演练记录(季度演练)。"""

    drill_id: str
    started_at: float
    completed_at: float = 0.0
    success: bool = False
    shares_collected: int = 0
    reconstructed: bool = False
    notes: str = ""


class ShamirSecretSharing:
    """Shamir Secret Sharing 简化实现(GF(256))。

    将 secret 分成 N 份,任意 K 份可重建。
    基于 polynomial:secret = f(0),分片 = f(i),重建用 Lagrange 插值。

    注意:生产环境推荐用 `pycryptodomex` 或 `secret-sharing` 库,
    此处为简化版(GF(2^8)运算)。
    """

    # GF(2^8) 生成多项式(0x11B = x^8 + x^4 + x^3 + x + 1)
    GF_MOD = 0x11B

    @staticmethod
    def _gf_mul(a: int, b: int) -> int:
        """GF(2^8) 乘法。"""
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi_bit = a & 0x80
            a = (a << 1) & 0xFF
            if hi_bit:
                a ^= 0x1B  # irreducible polynomial
            b >>= 1
        return p

    @staticmethod
    def _gf_pow(a: int, n: int) -> int:
        """GF(2^8) 幂。"""
        result = 1
        for _ in range(n):
            result = ShamirSecretSharing._gf_mul(result, a)
        return result

    @staticmethod
    def _gf_inv(a: int) -> int:
        """GF(2^8) 逆元(暴力版,256 个元素,可接受)。"""
        if a == 0:
            return 0
        for b in range(1, 256):
            if ShamirSecretSharing._gf_mul(a, b) == 1:
                return b
        return 0  # unreachable

    @staticmethod
    def split(secret: bytes, n: int, k: int) -> list[tuple[int, bytes]]:
        """分片:返回 [(index, share_bytes), ...]。

        secret 长度可任意,逐字节做 SSS。
        """
        if k > n:
            raise ValueError("k must be <= n")
        if k < 2:
            raise ValueError("k must be >= 2")
        if n > 255:
            raise ValueError("n must be <= 255")

        # 为每个 byte 生成随机多项式(常数项 = secret_byte)
        # f(x) = secret_byte + a1*x + a2*x^2 + ... + a(k-1)*x^(k-1)
        # share_i = f(i) for i in 1..n
        shares: list[bytearray] = [bytearray() for _ in range(n)]
        for byte in secret:
            # 随机系数(k-1 个,a0 = byte)
            coeffs = [byte] + [secrets.randbelow(256) for _ in range(k - 1)]
            # 计算每个分片
            for i in range(1, n + 1):
                # f(i) = sum(coeffs[j] * i^j) mod GF
                val = 0
                for j, c in enumerate(coeffs):
                    val ^= ShamirSecretSharing._gf_mul(c, ShamirSecretSharing._gf_pow(i, j))
                shares[i - 1].append(val)

        return [(i + 1, bytes(share)) for i, share in enumerate(shares)]

    @staticmethod
    def reconstruct(shares: list[tuple[int, bytes]]) -> bytes:
        """重建:Lagrange 插值。"""
        if len(shares) < 2:
            raise ValueError("need at least 2 shares to reconstruct")

        # 所有 share 长度应一致
        length = len(shares[0][1])
        result = bytearray()
        for byte_idx in range(length):
            # 每个字节单独插值
            points = [(x, share[byte_idx]) for x, share in shares]
            # f(0) = sum(y_i * prod(x_j / (x_j - x_i))) for i != j
            secret_byte = 0
            for i, (xi, yi) in enumerate(points):
                # 计算 Lagrange 系数 L_i(0) = prod(-xj / (xi - xj)) for j != i
                num = 1
                den = 1
                for j, (xj, _) in enumerate(points):
                    if i == j:
                        continue
                    # GF(2^8):减法 = 异或;除法 = 乘以逆元
                    # L_i(0) = prod((0 - xj) / (xi - xj)) = prod(xj / (xi ^ xj))
                    num = ShamirSecretSharing._gf_mul(num, xj)
                    den = ShamirSecretSharing._gf_mul(den, xi ^ xj)
                # 项 = yi * num / den
                if den == 0:
                    raise ValueError("duplicate share index")
                term = ShamirSecretSharing._gf_mul(yi, ShamirSecretSharing._gf_mul(num, ShamirSecretSharing._gf_inv(den)))
                secret_byte ^= term
            result.append(secret_byte)
        return bytes(result)


class MasterKeyBackup:
    """主密钥备份管理器(SSS 分片 + 演练)。"""

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or DEFAULT_BACKUP_PATH
        self._lock = threading.RLock()
        # 状态持久化
        self._status: BackupStatus = BackupStatus.NOT_INITIALIZED
        self._shares: list[KeyShare] = []
        self._drills: list[DrillRecord] = []
        self._master_key_fingerprint: str = ""  # 主密钥指纹(用于验证重建正确)
        self._last_rotated_at: float = 0.0
        self._loaded = False

    # ==================================================================
    # 创建备份
    # ==================================================================

    def create_backup(
        self,
        master_key: bytes,
        n: int = DEFAULT_N_SHARES,
        k: int = DEFAULT_K_THRESHOLD,
        recipients: list[str] | None = None,
    ) -> list[KeyShare]:
        """为主密钥创建 SSS 备份。

        Args:
            master_key: 主密钥(二进制)
            n: 分片数(默认 5)
            k: 重建阈值(默认 3)
            recipients: 保管人列表(长度应 = n)

        Returns:
            分片列表(应分发给各保管人)
        """
        if not is_enabled("defense"):
            logger.warning("Master key backup skipped (defense disabled)")
            return []

        if recipients and len(recipients) != n:
            logger.warning("Recipients count != n, will distribute in order")

        with self._lock:
            self._load()
            # 计算主密钥指纹(SHA-256,前 16 字节)
            self._master_key_fingerprint = hashlib.sha256(master_key).hexdigest()[:32]
            # SSS 分片
            raw_shares = ShamirSecretSharing.split(master_key, n, k)
            # 包装为 KeyShare
            now = time.time()
            self._shares = []
            for idx, (share_index, share_value) in enumerate(raw_shares):
                recipient = recipients[idx] if recipients and idx < len(recipients) else f"recipient_{idx + 1}"
                self._shares.append(KeyShare(
                    share_id=f"share-{idx + 1:02d}",
                    share_index=share_index,
                    share_value=share_value.hex(),
                    recipient=recipient,
                    distributed_at=now,
                ))
            self._status = BackupStatus.BACKED_UP
            self._last_rotated_at = now
            self._save()
            logger.info(
                "Master key backed up: %d shares (threshold=%d), fingerprint=%s...",
                n, k, self._master_key_fingerprint[:8],
            )
            return list(self._shares)

    # ==================================================================
    # 重建主密钥
    # ==================================================================

    def reconstruct(self, shares: list[KeyShare]) -> bytes:
        """从分片重建主密钥(需至少 K 个分片)。

        Raises:
            ValueError: 分片不足 / 指纹不匹配
        """
        if len(shares) < 2:
            raise ValueError(f"Need at least 2 shares, got {len(shares)}")

        with self._lock:
            self._load()
            raw_shares = [
                (s.share_index, bytes.fromhex(s.share_value))
                for s in shares
            ]
            master_key = ShamirSecretSharing.reconstruct(raw_shares)

            # 指纹验证(防止分片伪造 / 损坏)
            fingerprint = hashlib.sha256(master_key).hexdigest()[:32]
            if self._master_key_fingerprint and fingerprint != self._master_key_fingerprint:
                raise ValueError(
                    f"Master key fingerprint mismatch: expected {self._master_key_fingerprint[:8]}..., "
                    f"got {fingerprint[:8]}..."
                )

            self._status = BackupStatus.RECOVERED
            self._save()
            logger.info("Master key reconstructed from %d shares", len(shares))
            return master_key

    # ==================================================================
    # 演练
    # ==================================================================

    def drill(
        self,
        shares_to_use: list[KeyShare] | None = None,
        notes: str = "",
    ) -> DrillRecord:
        """演练:验证分片可重建主密钥。

        Args:
            shares_to_use: 用于演练的分片(默认用所有已分发分片的子集)
            notes: 演练备注

        Returns:
            DrillRecord: 演练结果
        """
        if not is_enabled("defense"):
            return DrillRecord(
                drill_id=f"drill-{int(time.time())}",
                started_at=time.time(),
                notes="defense disabled",
            )

        with self._lock:
            self._load()
            now = time.time()
            drill = DrillRecord(
                drill_id=f"drill-{int(now)}",
                started_at=now,
            )
            try:
                # 选择分片(默认 K 个)
                if shares_to_use is None:
                    available = [s for s in self._shares if s.share_value]
                    if len(available) < 2:
                        drill.notes = "insufficient shares for drill"
                        self._drills.append(drill)
                        self._save()
                        return drill
                    # 取前 K 个(模拟真实重建)
                    shares_to_use = available[:3]
                master_key = self.reconstruct(shares_to_use)
                drill.shares_collected = len(shares_to_use)
                drill.reconstructed = True
                # 验证指纹(已在 reconstruct 内做)
                drill.success = True
                drill.completed_at = time.time()
                if not notes:
                    notes = "quarterly drill"
                drill.notes = notes
                self._status = BackupStatus.DRILL_VERIFIED
            except Exception as e:
                drill.success = False
                drill.completed_at = time.time()
                drill.notes = f"drill failed: {e}"
                logger.error("Master key drill failed: %s", e)
            self._drills.append(drill)
            self._save()
            # 注意:演练后立即销毁内存中的 master_key(不再使用)
            del master_key
            return drill

    # ==================================================================
    # 轮换
    # ==================================================================

    def rotate(
        self,
        new_master_key: bytes,
        recipients: list[str] | None = None,
        n: int = DEFAULT_N_SHARES,
        k: int = DEFAULT_K_THRESHOLD,
    ) -> list[KeyShare]:
        """轮换主密钥(旧分片作废)。"""
        with self._lock:
            self._load()
            # 旧分片作废(保留历史审计,但不可用于重建)
            old_shares = list(self._shares)
            # 创建新备份(覆盖)
            new_shares = self.create_backup(
                new_master_key, n=n, k=k, recipients=recipients
            )
            self._status = BackupStatus.ROTATED
            self._save()
            logger.info(
                "Master key rotated: %d old shares invalidated, %d new shares distributed",
                len(old_shares), len(new_shares),
            )
            return new_shares

    # ==================================================================
    # 查询
    # ==================================================================

    def has_backup(self) -> bool:
        with self._lock:
            self._load()
            return bool(self._shares) and self._status != BackupStatus.NOT_INITIALIZED

    def get_status(self) -> BackupStatus:
        with self._lock:
            self._load()
            return self._status

    def list_shares(self) -> list[dict[str, Any]]:
        """列出分片(不含 share_value,防泄漏)。"""
        with self._lock:
            self._load()
            return [
                {
                    "share_id": s.share_id,
                    "share_index": s.share_index,
                    "recipient": s.recipient,
                    "distributed_at": s.distributed_at,
                    "received_back_at": s.received_back_at,
                }
                for s in self._shares
            ]

    def list_drills(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load()
            return [asdict(d) for d in self._drills]

    # ==================================================================
    # 内部
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            import json
            state_file = self.store_path / "state.json"
            if state_file.exists():
                data = json.loads(state_file.read_text(encoding="utf-8"))
                self._status = BackupStatus(data.get("status", "not_initialized"))
                self._master_key_fingerprint = data.get("master_key_fingerprint", "")
                self._last_rotated_at = data.get("last_rotated_at", 0.0)
                for s in data.get("shares", []):
                    self._shares.append(KeyShare(
                        share_id=s["share_id"],
                        share_index=s["share_index"],
                        share_value=s.get("share_value", ""),  # 分片值可单独加密存储
                        recipient=s.get("recipient", ""),
                        distributed_at=s.get("distributed_at", 0.0),
                        received_back_at=s.get("received_back_at"),
                    ))
                for d in data.get("drills", []):
                    self._drills.append(DrillRecord(
                        drill_id=d["drill_id"],
                        started_at=d.get("started_at", 0.0),
                        completed_at=d.get("completed_at", 0.0),
                        success=d.get("success", False),
                        shares_collected=d.get("shares_collected", 0),
                        reconstructed=d.get("reconstructed", False),
                        notes=d.get("notes", ""),
                    ))
        except Exception as e:
            logger.warning("MasterKeyBackup load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            import json
            self.store_path.mkdir(parents=True, exist_ok=True)
            state_file = self.store_path / "state.json"
            data = {
                "version": 1,
                "status": self._status.value,
                "master_key_fingerprint": self._master_key_fingerprint,
                "last_rotated_at": self._last_rotated_at,
                "shares": [
                    {
                        "share_id": s.share_id,
                        "share_index": s.share_index,
                        "share_value": s.share_value,
                        "recipient": s.recipient,
                        "distributed_at": s.distributed_at,
                        "received_back_at": s.received_back_at,
                    }
                    for s in self._shares
                ],
                "drills": [asdict(d) for d in self._drills],
            }
            tmp = state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, state_file)
        except Exception as e:
            logger.error("MasterKeyBackup save failed: %s", e)


# =====================================================================
# 全局单例
# =====================================================================

_backup_instance: MasterKeyBackup | None = None
_backup_lock = threading.Lock()


def get_master_key_backup() -> MasterKeyBackup:
    global _backup_instance
    if _backup_instance is None:
        with _backup_lock:
            if _backup_instance is None:
                _backup_instance = MasterKeyBackup()
    return _backup_instance
