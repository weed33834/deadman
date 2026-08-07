"""VaultStore - 数字遗产保险库

参考竞品：
    - My-Legacy.ai 零信任数字保险库
    - VoiceWill 加密保险库 + 受益人门户
    - GoodTrust Smart Digital Vault

设计要点：
    - 内容用 PBKDF2 派生密钥 + AES-256-GCM 认证加密（统一 utils.crypto 原语，
      nonce + ciphertext + tag 一体），向后兼容解密旧版 XOR/HMAC envelope。
    - 元数据中不存明文 content
    - 受益人只能看到自己被指定的条目
    - on_death 投递需要 7 天等待期 + 受益人手动确认
      遵守 rules/notification-guardrails.md 第二章约束 1（显式 opt-in）

遵守：
    - rules/legal-compliance-framework.md 第五章 PIPL：加密、去标识化
    - rules/integrity-framework.md：不编造投递进度
    - rules/safety-protocol.md：投递涉及自杀/非正常死亡触发 L0 时延后
    - rules/notification-guardrails.md：on_death 不自动投递

存储路径：
    ~/.deadman/vault/{user_id}/items/{item_id}.enc  # 加密内容
    ~/.deadman/vault/{user_id}/index.json            # 元数据（不含 content）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..utils import crypto
from ..utils.db_retry import best_effort_db_write

logger = logging.getLogger(__name__)


# =====================================================================
# 投递触发类型常量
# =====================================================================
TRIGGER_IMMEDIATE = "immediate"
TRIGGER_ON_DEATH = "on_death"
TRIGGER_ON_DATE = "on_date"
TRIGGER_MANUAL = "manual"

_VALID_TRIGGERS = {TRIGGER_IMMEDIATE, TRIGGER_ON_DEATH, TRIGGER_ON_DATE, TRIGGER_MANUAL}

# on_death 触发后的强制等待期（天）—— 防止误触、留出复核窗口
ON_DEATH_WAIT_DAYS = 7


# =====================================================================
# VaultItem 数据结构
# =====================================================================
@dataclass
class VaultItem:
    """保险库条目

    content_encrypted 为加密后的字节，不存明文。
    metadata 用于附加业务字段（如 account_platform、tags 等），
    但绝不含 content 明文。
    """

    item_id: str
    owner_user_id: str
    type: str  # password / document / photo / video / audio / note / account / crypto
    title: str
    content_encrypted: bytes
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    beneficiary_user_ids: list[str] = field(default_factory=list)
    delivery_trigger: str = TRIGGER_MANUAL
    delivery_date: datetime | None = None
    # 投递状态记录（不直接产生投递，仅记录触发时间 + 等待期起算）
    delivery_pending_since: datetime | None = None
    delivered_to: dict[str, str] = field(default_factory=dict)  # {beneficiary_id: delivered_at_iso}

    def to_index_dict(self) -> dict[str, Any]:
        """转成可序列化的索引条目（不含 content_encrypted）"""
        d = asdict(self)
        # bytes 不可 JSON 序列化，索引中本就不存 content
        d.pop("content_encrypted", None)
        # datetime 转 iso
        for k in ("created_at", "updated_at", "delivery_date", "delivery_pending_since"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif v is None:
                d[k] = None
        return d


# =====================================================================
# VaultStore
# =====================================================================
class VaultStore:
    """数字遗产保险库 - 加密存储 + 受益人指定 + 投递触发

    所有写入操作原子化（先写 .tmp 再 os.replace），失败仅 warning 不抛异常。
    所有读取操作对权限失败返回 None，避免泄露存在性。
    """

    # 派生密钥参数（PBKDF2-HMAC-SHA256）
    PBKDF2_ITERATIONS = 100_000
    PBKDF2_KEY_LEN = 32  # 256-bit
    PBKDF2_SALT_LEN = 16

    def __init__(self, data_dir: Path | None = None) -> None:
        """初始化保险库。

        Args:
            data_dir: 数据根目录，默认 ~/.deadman/vault/
        """
        if data_dir is None:
            data_dir = Path.home() / ".deadman" / "vault"
        self.data_dir: Path = Path(data_dir)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("VaultStore 创建数据目录失败 %s: %s", self.data_dir, exc)

    # ==================================================================
    # DB 双写辅助（企业级扩展④g）
    # ==================================================================
    # 策略与 CronScheduler / NotificationGuardrail 一致：
    #   - 写操作：文件存储成功后 best-effort 同步到 DB（消除全文件 read-modify-write 竞争）
    #   - 读操作：继续走文件存储（保持现有解密路径，避免引入双向转换 bug）
    #   - content_encrypted 以 LargeBinary 原样存，不解密、不改密钥派生

    @staticmethod
    def _db_enabled() -> bool:
        """是否启用主数据库（惰性检查，避免 import 时耦合）。"""
        try:
            from ..db.engine import db_enabled

            return db_enabled()
        except ImportError:
            return False

    @staticmethod
    def _run_async(coro):
        """在同步上下文执行异步协程；已在事件循环中时 fire-and-forget。"""
        import asyncio

        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(coro)  # noqa: RUF006 - 有意 fire-and-forget
        except RuntimeError:
            asyncio.run(coro)

    async def _sync_item_to_db(self, item: VaultItem) -> None:
        """upsert 单个 VaultItem 到 DB（best-effort，失败仅 warning）。"""

        async def _op() -> None:
            from ..db.engine import get_async_session_factory
            from ..db.models import VaultItem as VaultItemORM

            async with get_async_session_factory()() as session:
                existing = await session.get(VaultItemORM, item.item_id)
                if existing is not None:
                    existing.owner_user_id = item.owner_user_id
                    existing.type = item.type
                    existing.title = item.title
                    existing.content_encrypted = bytes(item.content_encrypted)
                    existing.item_metadata = dict(item.metadata)
                    existing.beneficiary_user_ids = list(item.beneficiary_user_ids)
                    existing.delivery_trigger = item.delivery_trigger
                    existing.delivery_date = item.delivery_date
                    existing.delivery_pending_since = item.delivery_pending_since
                    existing.delivered_to = dict(item.delivered_to)
                    existing.updated_at = item.updated_at
                else:
                    session.add(
                        VaultItemORM(
                            item_id=item.item_id,
                            owner_user_id=item.owner_user_id,
                            type=item.type,
                            title=item.title,
                            content_encrypted=bytes(item.content_encrypted),
                            item_metadata=dict(item.metadata),
                            beneficiary_user_ids=list(item.beneficiary_user_ids),
                            delivery_trigger=item.delivery_trigger,
                            delivery_date=item.delivery_date,
                            delivery_pending_since=item.delivery_pending_since,
                            delivered_to=dict(item.delivered_to),
                            created_at=item.created_at,
                            updated_at=item.updated_at,
                        )
                    )
                await session.commit()

        await best_effort_db_write(_op, "同步 vault item 到 DB", logger)

    async def _delete_item_from_db(self, item_id: str) -> None:
        """从 DB 删除单个 VaultItem（best-effort）。"""

        async def _op() -> None:
            from sqlalchemy import delete

            from ..db.engine import get_async_session_factory
            from ..db.models import VaultItem as VaultItemORM

            async with get_async_session_factory()() as session:
                await session.execute(
                    delete(VaultItemORM).where(VaultItemORM.item_id == item_id)
                )
                await session.commit()

        await best_effort_db_write(_op, "从 DB 删除 vault item", logger)

    # ==================================================================
    # 用户目录辅助
    # ==================================================================
    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / user_id

    def _items_dir(self, user_id: str) -> Path:
        d = self._user_dir(user_id) / "items"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_file(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "index.json"

    def _item_file(self, user_id: str, item_id: str) -> Path:
        return self._items_dir(user_id) / f"{item_id}.enc"

    # ==================================================================
    # 索引文件读写
    # ==================================================================
    def _read_index(self, user_id: str) -> dict[str, dict[str, Any]]:
        path = self._index_file(user_id)
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("VaultStore 读取索引失败 %s: %s", path, exc)
            return {}

    def _write_index(self, user_id: str, index: dict[str, dict[str, Any]]) -> None:
        path = self._index_file(user_id)
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("VaultStore 写入索引失败 %s: %s", path, exc)

    def _read_item_file(self, user_id: str, item_id: str) -> bytes:
        path = self._item_file(user_id, item_id)
        if not path.exists():
            return b""
        try:
            return path.read_bytes()
        except OSError as exc:
            logger.warning("VaultStore 读取条目文件失败 %s: %s", path, exc)
            return b""

    def _write_item_file(self, user_id: str, item_id: str, content: bytes) -> None:
        path = self._item_file(user_id, item_id)
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("VaultStore 写入条目文件失败 %s: %s", path, exc)

    # ==================================================================
    # 加密 - AES-256-GCM（统一使用 utils.crypto）
    # ==================================================================
    # v5.2 迁移：从手写 HMAC-SHA256 keystream 升级到 AES-256-GCM，
    # 消除 W2（手写弱密码学）问题。旧数据仍可解密（向后兼容）。
    def _derive_key(self, user_id: str, password: str) -> bytes:
        """从用户 ID + 主密码派生加密密钥（PBKDF2-HMAC-SHA256）

        Args:
            user_id: 用户 ID（作为 salt 的一部分）
            password: 主密码（来自环境变量或上游认证系统）

        Returns:
            32 字节派生密钥
        """
        salt = (user_id + "::deadman-vault").encode("utf-8")
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            self.PBKDF2_ITERATIONS,
            dklen=self.PBKDF2_KEY_LEN,
        )

    def _get_master_password(self) -> str:
        """获取主密码（来自环境变量 DEADMAN_VAULT_PASSWORD）

        未配置时使用固定的开发默认值并 warning，生产环境必须设置。
        """
        pw = os.getenv("DEADMAN_VAULT_PASSWORD", "")
        if not pw:
            logger.warning(
                "VaultStore: DEADMAN_VAULT_PASSWORD 未配置，使用开发默认密码。"
                "生产环境必须设置此环境变量。"
            )
            pw = "deadman-dev-default-password-not-for-production"
        return pw

    def _encrypt(self, plaintext: bytes, key: bytes) -> bytes:
        """AES-256-GCM 加密

        格式：nonce(12) || ciphertext + GCM tag
        """
        return crypto.encrypt_bytes(plaintext, key)

    def _decrypt(self, ciphertext: bytes, key: bytes) -> bytes:
        """AES-256-GCM 解密；向后兼容旧 HMAC keystream 格式

        旧格式：nonce(16) || ciphertext || tag(32)（HMAC-SHA256 keystream）
        新格式：nonce(12) || ciphertext + tag（AES-256-GCM）

        校验失败返回空 bytes（不抛异常），与原接口一致。
        """
        # 先尝试新格式（AES-256-GCM）
        try:
            return crypto.decrypt_bytes(ciphertext, key)
        except Exception:
            pass
        # 回退到旧格式（HMAC-SHA256 keystream）
        return self._decrypt_v1(ciphertext, key)

    def _decrypt_v1(self, ciphertext: bytes, key: bytes) -> bytes:
        """旧格式兼容解密（HMAC-SHA256 keystream，v5.2 之前的数据迁移用）"""
        import hashlib as _hl
        import hmac

        if len(ciphertext) < self.PBKDF2_SALT_LEN + 32:
            return b""
        nonce = ciphertext[: self.PBKDF2_SALT_LEN]
        tag = ciphertext[-32:]
        body = ciphertext[self.PBKDF2_SALT_LEN : -32]
        expected_tag = hmac.new(key, nonce + body, _hl.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            logger.warning("VaultStore: HMAC 校验失败，可能被篡改或密钥错误")
            return b""
        # 重生成 keystream
        keystream = bytearray()
        counter = 0
        while len(keystream) < len(body):
            block = hmac.new(key, nonce + counter.to_bytes(4, "big"), _hl.sha256).digest()
            keystream.extend(block)
            counter += 1
        keystream = keystream[: len(body)]
        return bytes(c ^ k for c, k in zip(body, keystream, strict=True))

    # ==================================================================
    # 条目 CRUD
    # ==================================================================
    def add_item(
        self,
        owner_user_id: str,
        type: str,
        title: str,
        content: bytes | str,
        beneficiary_user_ids: list[str],
        delivery_trigger: str = TRIGGER_MANUAL,
        delivery_date: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VaultItem:
        """添加条目（自动加密）"""
        if delivery_trigger not in _VALID_TRIGGERS:
            raise ValueError(f"无效 delivery_trigger: {delivery_trigger}")
        if delivery_trigger == TRIGGER_ON_DATE and delivery_date is None:
            raise ValueError("on_date 触发必须提供 delivery_date")

        content_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)

        item_id = f"item-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        key = self._derive_key(owner_user_id, self._get_master_password())
        encrypted = self._encrypt(content_bytes, key)

        item = VaultItem(
            item_id=item_id,
            owner_user_id=owner_user_id,
            type=type,
            title=title,
            content_encrypted=encrypted,
            metadata=dict(metadata) if metadata else {},
            created_at=now,
            updated_at=now,
            beneficiary_user_ids=list(beneficiary_user_ids),
            delivery_trigger=delivery_trigger,
            delivery_date=delivery_date,
        )

        # 写入加密内容文件
        self._write_item_file(owner_user_id, item_id, encrypted)
        # 写入索引（不含 content）
        index = self._read_index(owner_user_id)
        index[item_id] = item.to_index_dict()
        self._write_index(owner_user_id, index)
        # DB 双写（best-effort，消除全文件 read-modify-write 竞争）
        if self._db_enabled():
            self._run_async(self._sync_item_to_db(item))
        return item

    def _load_item(self, owner_user_id: str, item_id: str) -> VaultItem | None:
        """从索引 + 文件加载完整 VaultItem（含加密内容）"""
        index = self._read_index(owner_user_id)
        entry = index.get(item_id)
        if not entry:
            return None
        encrypted = self._read_item_file(owner_user_id, item_id)
        if not encrypted:
            return None
        return self._entry_to_item(entry, encrypted)

    @staticmethod
    def _entry_to_item(entry: dict[str, Any], encrypted: bytes) -> VaultItem:
        """把索引条目 + 加密内容转回 VaultItem"""

        def _parse_dt(v: Any) -> datetime | None:
            if not v:
                return None
            try:
                return datetime.fromisoformat(v)
            except (TypeError, ValueError):
                return None

        return VaultItem(
            item_id=entry["item_id"],
            owner_user_id=entry["owner_user_id"],
            type=entry["type"],
            title=entry["title"],
            content_encrypted=encrypted,
            metadata=entry.get("metadata", {}) or {},
            created_at=_parse_dt(entry.get("created_at"))
            or datetime.now(timezone.utc).replace(tzinfo=None),
            updated_at=_parse_dt(entry.get("updated_at"))
            or datetime.now(timezone.utc).replace(tzinfo=None),
            beneficiary_user_ids=list(entry.get("beneficiary_user_ids", []) or []),
            delivery_trigger=entry.get("delivery_trigger", TRIGGER_MANUAL),
            delivery_date=_parse_dt(entry.get("delivery_date")),
            delivery_pending_since=_parse_dt(entry.get("delivery_pending_since")),
            delivered_to=dict(entry.get("delivered_to", {}) or {}),
        )

    def get_item(self, item_id: str, requester_user_id: str) -> VaultItem | None:
        """获取条目

        权限：
            - owner 可获取自己所有条目
            - beneficiary 只能获取自己被指定的条目
            - 其他人返回 None（不泄露存在性）
        """
        # 先找 owner：遍历可能很慢，所以索引里我们记了 owner_user_id
        # 但调用者通常知道 item_id 而不知道 owner——这里要求调用方提供 owner 上下文
        # 改为：扫描所有用户目录（数据规模小，可接受）
        # 但更稳的方式是调用方按场景调用：owner 用 list_items 拿 item_id；
        # beneficiary 用 list_inherited 拿 item_id。
        # 这里采取两段式：先查 requester 自己的索引（owner 路径）
        owner_item = self._load_item(requester_user_id, item_id)
        if owner_item is not None:
            return owner_item
        # 否则作为 beneficiary 查所有可能的 owner
        for user_dir in self.data_dir.iterdir():
            if not user_dir.is_dir():
                continue
            owner_id = user_dir.name
            if owner_id == requester_user_id:
                continue
            item = self._load_item(owner_id, item_id)
            if item is None:
                continue
            if requester_user_id in item.beneficiary_user_ids:
                return item
        return None

    def list_items(self, owner_user_id: str, requester_user_id: str) -> list[dict[str, Any]]:
        """列出条目（仅元数据，不含 content）

        权限：
            - owner 看自己所有
            - beneficiary 只看自己被指定的（通过 list_inherited）
        这里 requester==owner 时返回全部；其他情况返回空（beneficiary 走 list_inherited）
        """
        if requester_user_id != owner_user_id:
            return []
        index = self._read_index(owner_user_id)
        return list(index.values())

    def update_item(
        self, item_id: str, owner_user_id: str, updates: dict[str, Any]
    ) -> VaultItem | None:
        """更新条目（仅 owner 可改）

        可更新字段：title / content / metadata / beneficiary_user_ids /
                   delivery_trigger / delivery_date
        """
        item = self._load_item(owner_user_id, item_id)
        if item is None:
            return None

        if "title" in updates:
            item.title = str(updates["title"])
        if "metadata" in updates:
            item.metadata = dict(updates["metadata"]) if updates["metadata"] else {}
        if "beneficiary_user_ids" in updates:
            item.beneficiary_user_ids = list(updates["beneficiary_user_ids"])
        if "delivery_trigger" in updates:
            new_trigger = updates["delivery_trigger"]
            if new_trigger not in _VALID_TRIGGERS:
                raise ValueError(f"无效 delivery_trigger: {new_trigger}")
            item.delivery_trigger = new_trigger
        if "delivery_date" in updates:
            item.delivery_date = updates["delivery_date"]
        if "content" in updates:
            content = updates["content"]
            content_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            key = self._derive_key(owner_user_id, self._get_master_password())
            item.content_encrypted = self._encrypt(content_bytes, key)
            self._write_item_file(owner_user_id, item_id, item.content_encrypted)

        item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        index = self._read_index(owner_user_id)
        index[item_id] = item.to_index_dict()
        self._write_index(owner_user_id, index)
        # DB 双写（best-effort）
        if self._db_enabled():
            self._run_async(self._sync_item_to_db(item))
        return item

    def delete_item(self, item_id: str, owner_user_id: str) -> bool:
        """删除条目（仅 owner 可删）"""
        index = self._read_index(owner_user_id)
        if item_id not in index:
            return False
        del index[item_id]
        self._write_index(owner_user_id, index)
        # 删除加密内容文件
        path = self._item_file(owner_user_id, item_id)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("VaultStore 删除条目文件失败 %s: %s", path, exc)
        # DB 双写删除（best-effort）
        if self._db_enabled():
            self._run_async(self._delete_item_from_db(item_id))
        return True

    # ==================================================================
    # 受益人视图
    # ==================================================================
    def list_beneficiaries(self, owner_user_id: str) -> list[dict[str, Any]]:
        """列出我指定的所有受益人（去重 + 统计每个受益人被指定几条）"""
        index = self._read_index(owner_user_id)
        counter: dict[str, int] = {}
        for entry in index.values():
            for bid in entry.get("beneficiary_user_ids", []) or []:
                counter[bid] = counter.get(bid, 0) + 1
        return [
            {"beneficiary_user_id": bid, "item_count": cnt} for bid, cnt in sorted(counter.items())
        ]

    def list_inherited(self, beneficiary_user_id: str) -> list[dict[str, Any]]:
        """列出我能继承的条目（未到投递时间的也列出，但标记 pending）

        返回每条含字段：
            item_id / owner_user_id / title / type / delivery_trigger /
            delivery_date / status (pending / deliverable / delivered)
        """
        results: list[dict[str, Any]] = []
        if not self.data_dir.exists():
            return results
        for user_dir in self.data_dir.iterdir():
            if not user_dir.is_dir():
                continue
            owner_id = user_dir.name
            index = self._read_index(owner_id)
            for entry in index.values():
                if beneficiary_user_id not in (entry.get("beneficiary_user_ids") or []):
                    continue
                status = self._compute_delivery_status(entry, beneficiary_user_id)
                results.append(
                    {
                        "item_id": entry["item_id"],
                        "owner_user_id": owner_id,
                        "title": entry.get("title", ""),
                        "type": entry.get("type", ""),
                        "delivery_trigger": entry.get("delivery_trigger", TRIGGER_MANUAL),
                        "delivery_date": entry.get("delivery_date"),
                        "status": status,
                    }
                )
        return results

    @staticmethod
    def _compute_delivery_status(entry: dict[str, Any], beneficiary_id: str) -> str:
        """根据触发类型 + 等待期计算当前状态"""
        delivered_to = entry.get("delivered_to") or {}
        if beneficiary_id in delivered_to:
            return "delivered"
        trigger = entry.get("delivery_trigger", TRIGGER_MANUAL)
        if trigger in (TRIGGER_IMMEDIATE, TRIGGER_MANUAL):
            return "deliverable"
        if trigger == TRIGGER_ON_DEATH:
            pending_since = entry.get("delivery_pending_since")
            if not pending_since:
                return "pending"  # 死亡触发未启动
            try:
                since = datetime.fromisoformat(pending_since)
            except (TypeError, ValueError):
                return "pending"
            if datetime.now(timezone.utc).replace(tzinfo=None) - since >= timedelta(
                days=ON_DEATH_WAIT_DAYS
            ):
                return "deliverable"
            return "pending"
        if trigger == TRIGGER_ON_DATE:
            d = entry.get("delivery_date")
            if not d:
                return "pending"
            try:
                target = datetime.fromisoformat(d)
            except (TypeError, ValueError):
                return "pending"
            if datetime.now(timezone.utc).replace(tzinfo=None) >= target:
                return "deliverable"
            return "pending"
        return "pending"

    # ==================================================================
    # 投递触发
    # ==================================================================
    def trigger_delivery(
        self,
        item_id: str,
        trigger_type: str,
        requester_user_id: str,
    ) -> dict[str, Any]:
        """触发投递

        trigger_type:
            - on_death: 标记进入 7 天等待期（需受益人后续再次调用确认投递）
            - on_date:  检查 delivery_date 是否已到，到则投递
            - manual:   立即投递（owner 或 beneficiary 都可调）

        返回 {delivered: bool, content: bytes | None,
              pending_days: int, reason: str}
        """
        # 找到 item
        item = self.get_item(item_id, requester_user_id)
        if item is None:
            return {
                "delivered": False,
                "content": None,
                "pending_days": 0,
                "reason": "not_found_or_unauthorized",
            }

        is_owner = requester_user_id == item.owner_user_id
        is_beneficiary = requester_user_id in item.beneficiary_user_ids
        if not (is_owner or is_beneficiary):
            return {
                "delivered": False,
                "content": None,
                "pending_days": 0,
                "reason": "unauthorized",
            }

        # on_death: 第一次触发只记 pending_since，进入 7 天等待
        if trigger_type == TRIGGER_ON_DEATH:
            if item.delivery_pending_since is None:
                # 仅 owner 可启动等待期
                if not is_owner:
                    return {
                        "delivered": False,
                        "content": None,
                        "pending_days": 0,
                        "reason": "only_owner_can_start_death_wait",
                    }
                item.delivery_pending_since = datetime.now(timezone.utc).replace(tzinfo=None)
                item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                index = self._read_index(item.owner_user_id)
                index[item_id] = item.to_index_dict()
                self._write_index(item.owner_user_id, index)
                # DB 双写（best-effort，更新 delivery_pending_since）
                if self._db_enabled():
                    self._run_async(self._sync_item_to_db(item))
                return {
                    "delivered": False,
                    "content": None,
                    "pending_days": ON_DEATH_WAIT_DAYS,
                    "reason": "death_wait_started",
                }
            # 已有 pending_since，检查是否到 7 天
            elapsed = datetime.now(timezone.utc).replace(tzinfo=None) - item.delivery_pending_since
            remaining = timedelta(days=ON_DEATH_WAIT_DAYS) - elapsed
            if remaining > timedelta(0):
                return {
                    "delivered": False,
                    "content": None,
                    "pending_days": remaining.days + (1 if remaining.seconds > 0 else 0),
                    "reason": "in_death_wait_period",
                }
            # 等待期满：仅受益人可确认领取（owner 不能替受益人确认）
            if not is_beneficiary:
                return {
                    "delivered": False,
                    "content": None,
                    "pending_days": 0,
                    "reason": "awaiting_beneficiary_confirmation",
                }
            return self._do_deliver(item, requester_user_id)

        if trigger_type == TRIGGER_ON_DATE:
            if item.delivery_date is None:
                return {
                    "delivered": False,
                    "content": None,
                    "pending_days": 0,
                    "reason": "no_delivery_date_set",
                }
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if now < item.delivery_date:
                delta = item.delivery_date - now
                return {
                    "delivered": False,
                    "content": None,
                    "pending_days": delta.days + (1 if delta.seconds > 0 else 0),
                    "reason": "date_not_reached",
                }
            return self._do_deliver(item, requester_user_id)

        if trigger_type == TRIGGER_MANUAL:
            return self._do_deliver(item, requester_user_id)

        return {
            "delivered": False,
            "content": None,
            "pending_days": 0,
            "reason": f"unknown_trigger_type:{trigger_type}",
        }

    def _do_deliver(self, item: VaultItem, beneficiary_id: str) -> dict[str, Any]:
        """执行投递：解密 content 并标记已投递"""
        key = self._derive_key(item.owner_user_id, self._get_master_password())
        plaintext = self._decrypt(item.content_encrypted, key)
        if not plaintext:
            return {
                "delivered": False,
                "content": None,
                "pending_days": 0,
                "reason": "decrypt_failed",
            }
        # 标记已投递
        item.delivered_to[beneficiary_id] = (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        )
        item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        index = self._read_index(item.owner_user_id)
        index[item.item_id] = item.to_index_dict()
        self._write_index(item.owner_user_id, index)
        # DB 双写（best-effort，更新 delivered_to）
        if self._db_enabled():
            self._run_async(self._sync_item_to_db(item))
        return {
            "delivered": True,
            "content": plaintext,
            "pending_days": 0,
            "reason": "delivered",
        }
