"""终活笔记存储 - 加密 + PII 脱敏 + 共享 + 投递触发

存储路径：
    ~/.deadman/ending_notes/{user_id}/note.json   - 加密的笔记主体
    ~/.deadman/ending_notes/{user_id}/shares.json - 我的对外共享清单
    ~/.deadman/ending_notes/{user_id}/incoming.json - 共享给我的清单（owner_user_id 列表）
    ~/.deadman/ending_notes/{user_id}/pending_deliveries.json - 待投递（7 天等待期）

PIPL 合规（legal-compliance-framework.md 第五章）：
    - 文件级加密（密钥从用户密码派生；PBKDF2-HMAC-SHA256 派生 + AES-256-GCM 认证加密，
      统一 utils.crypto 原语；向后兼容解密旧版 XOR/HMAC envelope）
    - PII 字段（full_name/birth_date/contact/phone/account）落盘前由
      EndingNoteGuide._mask_pii 掩码；本存储层不再做二次掩码
    - 共享时仅传递已脱敏的笔记正文，不向家庭成员泄露其他成员的 PII

notification-guardrails 合规：
    - delivery_triggers 不自动触发，仅用户主动询问时显示
    - 死亡确认 trigger 需要 7 天等待期（避免情绪冲动决策）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from ..utils import crypto
from ..utils.db_retry import best_effort_db_write
from ..utils.jsonio import atomic_write_json, read_json
from .models import EndingNote

logger = logging.getLogger(__name__)

# 沿用本模块旧名，读失败时记录警告（jsonio 的 read_json 支持注入 logger）
_atomic_write_json = atomic_write_json
_read_json = partial(read_json, logger=logger)


# ====================================================================
# 加密原语 - 统一使用 utils.crypto（AES-256-GCM）
# ====================================================================
# v5.2 迁移：从手写 HMAC-SHA256 keystream 流密码升级到 AES-256-GCM，
# 消除 R1（重复加密实现）和 W1（手写弱密码学）问题。
# 旧 v1/v2 envelope 仍可解密（向后兼容），新写入一律用 v3（AES-GCM）。
# 加密接口签名保持不变：encrypt(plaintext, passphrase) -> envelope
# ====================================================================

_encrypt = crypto.encrypt_envelope
_decrypt = crypto.decrypt_envelope


def _get_passphrase(user_id: str | None = None, tenant_id: str | None = None) -> bytes:
    """取加密口令（Phase 14：返回 bytes，支持 per-tenant 派生）

    优先级：
    1. 环境变量 DEADMAN_ENDING_NOTE_PASSPHRASE（全局口令，开发期可用）
    2. per-tenant 派生（To B）：多租户模式下用 tenant_id 派生独立口令
       （HMAC(secret, "tenant:"+tenant_id+":ending-note")）
    3. per-user 派生（C 端）：仅当未处于多租户模式时用 user_id 派生
       （single 模式保持与 Phase 14 完全一致，零迁移向后兼容）
    4. 开发默认值（仅供测试，生产禁用）

    ⚠️ 设计说明：
       - single 模式（默认）：沿用 per-user 派生，既有加密数据直接可读，
         不破坏 C 端零迁移承诺。
       - multi 模式：派生改为 per-tenant，租户间即使同一 user_id 也互不
         可解密；旧 per-user 数据仍可读（load 时 fallback _legacy_user_passphrase），
         由迁移 CLI（deadman org migrate-crypto）统一重加密。
    """
    global_secret = os.environ.get(
        "DEADMAN_ENDING_NOTE_PASSPHRASE",
        "deadman-ending-note-dev-passphrase",
    )
    if global_secret == "deadman-ending-note-dev-passphrase":
        logger.warning(
            "EndingNoteStore: 使用开发默认口令，生产环境必须设置 "
            "DEADMAN_ENDING_NOTE_PASSPHRASE 环境变量"
        )
    from ..infrastructure.multi_tenant import get_current_tenant_id, is_multi_tenant_enabled

    if tenant_id is None and is_multi_tenant_enabled():
        tenant_id = get_current_tenant_id()
    if tenant_id:
        # per-tenant 派生：HMAC-SHA256(global_secret, "tenant:"+tenant_id+":ending-note")
        # 租户间数据隔离，即使全局 secret 泄露，也需知道 tenant_id 才能解密
        return hmac.new(
            global_secret.encode("utf-8"),
            ("tenant:" + tenant_id + ":ending-note").encode("utf-8"),
            hashlib.sha256,
        ).digest()
    if user_id:
        # per-user 派生（single 模式向后兼容）：HMAC-SHA256(global_secret, user_id)
        per_user = hmac.new(
            global_secret.encode("utf-8"),
            ("ending-note:" + user_id).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return per_user
    return global_secret.encode("utf-8")


def _legacy_user_passphrase(user_id: str) -> bytes:
    """旧 per-user 派生密钥（single 模式 / 迁移期兼容读）。"""
    global_secret = os.environ.get(
        "DEADMAN_ENDING_NOTE_PASSPHRASE",
        "deadman-ending-note-dev-passphrase",
    )
    return hmac.new(
        global_secret.encode("utf-8"),
        ("ending-note:" + user_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()


# ====================================================================
# 死亡确认投递的等待期（notification-guardrails.md 第一章约束 8）
# ====================================================================
DEATH_CONFIRMATION_WAIT_DAYS = 7


def _decrypt_v1(envelope: dict[str, Any]) -> bytes:
    """v1 兼容解密路径（Phase 14 之前的 envelope，无口令加密）

    v1 envelope 的 enc_key/mac_key 仅由随机 nonce+salt 派生，
    任何人都能解密 —— 仅用于读取旧数据并迁移到 v3，不应再用于新写入。
    """
    import base64

    _V1_KDF_ITERATIONS = 100_000
    _V1_KEY_LEN = 32

    nonce = base64.b64decode(envelope["nonce"])
    salt = base64.b64decode(envelope["salt"])
    ct = base64.b64decode(envelope["ct"])
    tag = base64.b64decode(envelope["tag"])

    # v1 的派生方式（与原 _derive_key 一致，仅供兼容读取）
    enc_key = hashlib.pbkdf2_hmac(
        "sha256",
        ("enc:" + base64.b16encode(salt).decode()).encode("utf-8"),
        nonce + salt,
        _V1_KDF_ITERATIONS,
        dklen=_V1_KEY_LEN,
    )
    mac_key = hashlib.pbkdf2_hmac(
        "sha256",
        ("mac:" + base64.b16encode(salt).decode()).encode("utf-8"),
        nonce + salt,
        _V1_KDF_ITERATIONS,
        dklen=_V1_KEY_LEN,
    )

    expected_tag = hmac.new(mac_key, ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, tag):
        raise ValueError("v1 HMAC tag 校验失败：文件已被篡改")

    # v1 keystream（HMAC-SHA256 counter mode）
    out = bytearray()
    counter = 0
    while len(out) < len(ct):
        block = hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    keystream = bytes(out[: len(ct)])
    return bytes(a ^ b for a, b in zip(ct, keystream, strict=True))


class EndingNoteStore:
    """终活笔记存储 - 加密 + PII 脱敏 + 共享 + 投递触发

    存储路径：~/.deadman/ending_notes/{user_id}/
        note.json              - 加密 envelope（包含完整 EndingNote JSON）
        shares.json            - 我共享给谁 [{target_user_id, sections, shared_at}]
        incoming.json          - 谁共享给我 [{owner_user_id, sections, shared_at}]
        pending_deliveries.json - 待投递 [{trigger_type, triggered_at, deliver_at,
                                           recipients, status}]

    PIPL 合规：
        - 文件级加密（密钥从用户密码派生，PBKDF2 + AES-256-GCM）
        - PII 字段在落盘前由 EndingNoteGuide._mask_pii 掩码
        - 共享时仅传递已脱敏的笔记正文，不向家庭成员泄露其他成员的 PII

    notification-guardrails 合规：
        - delivery_triggers 不自动触发，仅用户主动询问时显示
        - 死亡确认 trigger 需要 7 天等待期
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        """初始化存储

        Args:
            data_dir: 数据根目录，默认 ~/.deadman/ending_notes/（多租户时按租户路由）
        """
        from ..infrastructure.multi_tenant import resolve_tenant_path

        self.data_dir: Path = data_dir or resolve_tenant_path("ending_notes")

    # ------------------------------------------------------------------
    # DB 双写辅助（企业级扩展④i）
    # ------------------------------------------------------------------
    # 策略：写操作文件存储成功后 best-effort 同步到 DB；读操作走文件存储。
    #   - note envelope 整体序列化为 JSON 字符串存 Text 列（保留 v1/v2/v3 解密字段）
    #   - shares/incoming/pending_deliveries 拆为行级记录，消除全文件重写
    #   - 不解密、不改密钥派生

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

    async def _sync_note_to_db(self, user_id: str, envelope: dict[str, Any]) -> None:
        """upsert EndingNoteRecord 到 DB（envelope 序列化为 JSON 字符串，best-effort）。"""

        async def _op() -> None:
            from ..db.engine import get_async_session_factory
            from ..db.models import EndingNoteRecord

            envelope_text = json.dumps(envelope, ensure_ascii=False)
            async with get_async_session_factory()() as session:
                existing = await session.get(EndingNoteRecord, user_id)
                if existing is not None:
                    existing.envelope_text = envelope_text
                else:
                    session.add(EndingNoteRecord(user_id=user_id, envelope_text=envelope_text))
                await session.commit()

        await best_effort_db_write(_op, "同步 ending note 到 DB", logger)

    async def _delete_note_from_db(self, user_id: str) -> None:
        """从 DB 删除笔记及衍生记录（shares/incoming/pending_deliveries，best-effort）。"""

        async def _op() -> None:
            from sqlalchemy import delete

            from ..db.engine import get_async_session_factory
            from ..db.models import (
                EndingNoteIncoming,
                EndingNotePendingDelivery,
                EndingNoteRecord,
                EndingNoteShare,
            )

            async with get_async_session_factory()() as session:
                await session.execute(
                    delete(EndingNoteRecord).where(EndingNoteRecord.user_id == user_id)
                )
                # owner 维度删除：我共享给别人的
                await session.execute(
                    delete(EndingNoteShare).where(EndingNoteShare.owner_user_id == user_id)
                )
                # target 维度删除：别人共享给我的
                await session.execute(
                    delete(EndingNoteIncoming).where(EndingNoteIncoming.target_user_id == user_id)
                )
                await session.execute(
                    delete(EndingNotePendingDelivery).where(
                        EndingNotePendingDelivery.owner_user_id == user_id
                    )
                )
                await session.commit()

        await best_effort_db_write(_op, "从 DB 删除 ending note", logger)

    async def _sync_share_to_db(
        self,
        owner_user_id: str,
        target_user_id: str,
        sections: list[str] | None,
        shared_at: datetime,
    ) -> None:
        """upsert EndingNoteShare + EndingNoteIncoming（镜像双写，best-effort）。"""

        async def _op() -> None:
            from ..db.engine import get_async_session_factory
            from ..db.models import EndingNoteIncoming, EndingNoteShare

            share_id = f"{owner_user_id}:{target_user_id}"
            incoming_id = f"{target_user_id}:{owner_user_id}"
            async with get_async_session_factory()() as session:
                # owner → target 共享记录
                share = await session.get(EndingNoteShare, share_id)
                if share is not None:
                    share.sections = sections
                    share.shared_at = shared_at
                else:
                    session.add(
                        EndingNoteShare(
                            id=share_id,
                            owner_user_id=owner_user_id,
                            target_user_id=target_user_id,
                            sections=sections,
                            shared_at=shared_at,
                        )
                    )
                # target ← owner 接收记录（镜像）
                incoming = await session.get(EndingNoteIncoming, incoming_id)
                if incoming is not None:
                    incoming.sections = sections
                    incoming.shared_at = shared_at
                else:
                    session.add(
                        EndingNoteIncoming(
                            id=incoming_id,
                            target_user_id=target_user_id,
                            owner_user_id=owner_user_id,
                            sections=sections,
                            shared_at=shared_at,
                        )
                    )
                await session.commit()

        await best_effort_db_write(_op, "同步 ending note share 到 DB", logger)

    async def _delete_share_from_db(self, owner_user_id: str, target_user_id: str) -> None:
        """从 DB 删除 EndingNoteShare + EndingNoteIncoming（best-effort）。"""

        async def _op() -> None:
            from sqlalchemy import delete

            from ..db.engine import get_async_session_factory
            from ..db.models import EndingNoteIncoming, EndingNoteShare

            share_id = f"{owner_user_id}:{target_user_id}"
            incoming_id = f"{target_user_id}:{owner_user_id}"
            async with get_async_session_factory()() as session:
                await session.execute(delete(EndingNoteShare).where(EndingNoteShare.id == share_id))
                await session.execute(
                    delete(EndingNoteIncoming).where(EndingNoteIncoming.id == incoming_id)
                )
                await session.commit()

        await best_effort_db_write(_op, "从 DB 删除 ending note share", logger)

    async def _sync_pending_delivery_to_db(
        self,
        owner_user_id: str,
        trigger_type: str,
        triggered_at: datetime,
        deliver_at: datetime | None,
        recipients: list[str],
        status: str,
        delivered_at: datetime | None,
    ) -> None:
        """upsert EndingNotePendingDelivery 到 DB（按 owner+trigger 查询，best-effort）。

        每个 owner 每个 trigger_type 只有一条 pending 记录（与文件存储语义一致）。
        """

        async def _op() -> None:
            from sqlalchemy import select

            from ..db.engine import get_async_session_factory
            from ..db.models import EndingNotePendingDelivery

            async with get_async_session_factory()() as session:
                stmt = select(EndingNotePendingDelivery).where(
                    EndingNotePendingDelivery.owner_user_id == owner_user_id,
                    EndingNotePendingDelivery.trigger_type == trigger_type,
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing is not None:
                    existing.triggered_at = triggered_at
                    existing.deliver_at = deliver_at
                    existing.recipients = list(recipients)
                    existing.status = status
                    existing.delivered_at = delivered_at
                else:
                    import uuid

                    session.add(
                        EndingNotePendingDelivery(
                            id=f"{owner_user_id}:{trigger_type}:{uuid.uuid4().hex[:8]}",
                            owner_user_id=owner_user_id,
                            trigger_type=trigger_type,
                            triggered_at=triggered_at,
                            deliver_at=deliver_at,
                            recipients=list(recipients),
                            status=status,
                            delivered_at=delivered_at,
                        )
                    )
                await session.commit()

        await best_effort_db_write(_op, "同步 ending note pending delivery 到 DB", logger)

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / user_id

    def _note_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "note.json"

    def _shares_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "shares.json"

    def _incoming_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "incoming.json"

    def _pending_deliveries_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "pending_deliveries.json"

    # ------------------------------------------------------------------
    # 笔记 CRUD
    # ------------------------------------------------------------------

    def save(self, note: EndingNote) -> None:
        """保存笔记（加密 + 原子写入）

        - updated_at 由调用方负责（EndingNoteGuide.save_answer 会 touch）
        - PII 脱敏由 EndingNoteGuide._mask_pii 在写入字段时完成；本方法只负责加密
        - 文件级加密：plaintext -> _encrypt(plaintext, user_passphrase) -> envelope JSON
        - Phase 14：使用 per-user 派生的 passphrase 加密
        """
        note.touch()
        plaintext = json.dumps(note.to_dict(), ensure_ascii=False).encode("utf-8")
        passphrase = _get_passphrase(note.user_id)
        envelope = _encrypt(plaintext, passphrase)
        _atomic_write_json(self._note_path(note.user_id), envelope)
        # DB 双写（best-effort，envelope 整体存 Text 列，不解密）
        if self._db_enabled():
            self._run_async(self._sync_note_to_db(note.user_id, envelope))

    def load(self, user_id: str) -> EndingNote | None:
        """加载笔记；文件不存在返回 None

        加密解密路径（Step 4 per-tenant）：
          - multi 模式：优先用 per-tenant passphrase 解密；
            失败时 fallback 到旧 per-user 派生（迁移期兼容），成功即触发重加密。
          - single 模式：per-user 派生（与 Phase 14 完全一致，零迁移）。
        兼容旧版（version=1，无 passphrase）envelope：检测到 version=1 时
        尝试用 _decrypt_v1 解密（仅作向后兼容，旧数据应主动迁移到 v3）。
        """
        envelope = _read_json(self._note_path(user_id))
        if envelope is None:
            return None
        # Phase 14 兼容性：version=1 的旧 envelope 是无口令加密的
        # （任何人都能解密）。检测到旧版本时迁移到 v3。
        is_v1 = envelope.get("version", 1) == 1
        try:
            if is_v1:
                # 旧数据：用原 v1 算法的解密路径（仅读取，不重新加密）
                plaintext = _decrypt_v1(envelope)
            else:
                passphrase = _get_passphrase(user_id)
                try:
                    plaintext = _decrypt(envelope, passphrase)
                except ValueError:
                    # per-tenant 密钥解不开 → 旧 per-user 派生（迁移期兼容）
                    plaintext = _decrypt(envelope, _legacy_user_passphrase(user_id))
        except ValueError as e:
            logger.warning("解密笔记失败 user=%s: %s", user_id, e)
            return None
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("解析笔记 JSON 失败 user=%s: %s", user_id, e)
            return None
        note = EndingNote.from_dict(data)
        # v1 -> v3 自动迁移：解密成功后用新算法重新加密
        if is_v1:
            try:
                self.save(note)
                logger.info("已自动迁移 user=%s 笔记到 v3 加密", user_id)
            except Exception as e:
                logger.warning("v1->v3 迁移失败 user=%s: %s", user_id, e)
        return note

    def delete_section(self, user_id: str, section_key: str) -> bool:
        """删除（清空）笔记中的某个章节

        Args:
            user_id: 笔记所有者
            section_key: 章节 key（personal_info/family_relations/.../will_intent）

        Returns:
            True 如果章节被成功清空；False 如果笔记不存在
        """
        from .models import SECTION_KEYS

        note = self.load(user_id)
        if note is None:
            return False
        if section_key not in SECTION_KEYS:
            raise ValueError(f"未知章节 key: {section_key}")
        setattr(note, section_key, None)
        note.touch()
        self.save(note)
        return True

    def delete(self, user_id: str) -> bool:
        """删除笔记及其衍生文件（shares/incoming/pending_deliveries）

        Returns:
            True 如果笔记文件存在并被删除；False 如果笔记文件不存在
        """
        deleted = False
        for path in (
            self._note_path(user_id),
            self._shares_path(user_id),
            self._incoming_path(user_id),
            self._pending_deliveries_path(user_id),
        ):
            if path.exists():
                try:
                    path.unlink()
                    deleted = True
                except OSError as e:
                    logger.warning("删除文件失败 %s: %s", path, e)
        # 尝试清理空用户目录
        user_dir = self._user_dir(user_id)
        try:
            if user_dir.exists() and not any(user_dir.iterdir()):
                user_dir.rmdir()
        except OSError:
            pass
        # DB 双写删除（best-effort，删除 note + shares/incoming/pending_deliveries）
        if deleted and self._db_enabled():
            self._run_async(self._delete_note_from_db(user_id))
        return deleted

    # ------------------------------------------------------------------
    # 共享管理
    # ------------------------------------------------------------------

    def share_with(
        self,
        owner_user_id: str,
        target_user_id: str,
        sections: list[str] | None = None,
    ) -> None:
        """共享笔记给家庭成员

        Args:
            owner_user_id: 笔记所有者
            target_user_id: 接收方用户 ID
            sections: 指定章节共享，None = 全部章节
                支持 personal_info/family_relations/assets/funeral_wishes/
                medical_wishes/digital_legacy/messages/emergency_contacts/will_intent

        机制：
            - owner 的 shares.json 追加一条 {target_user_id, sections}
            - target 的 incoming.json 追加一条 {owner_user_id, sections}
            - 不复制笔记内容；读取时由 list_shared_with_me 实时调 owner 的 load
        """
        if owner_user_id == target_user_id:
            raise ValueError("不能与自己共享")

        shared_at = datetime.now()
        # owner 的对外共享清单
        shares = _read_json(self._shares_path(owner_user_id)) or []
        # 去重：已存在则更新 sections
        shares = [s for s in shares if s.get("target_user_id") != target_user_id]
        shares.append(
            {
                "target_user_id": target_user_id,
                "sections": sections,  # None 表示全部
                "shared_at": shared_at.isoformat(),
            }
        )
        _atomic_write_json(self._shares_path(owner_user_id), shares)

        # target 的 incoming 清单
        incoming = _read_json(self._incoming_path(target_user_id)) or []
        incoming = [s for s in incoming if s.get("owner_user_id") != owner_user_id]
        incoming.append(
            {
                "owner_user_id": owner_user_id,
                "sections": sections,
                "shared_at": shared_at.isoformat(),
            }
        )
        _atomic_write_json(self._incoming_path(target_user_id), incoming)
        # DB 双写（best-effort，镜像 upsert share + incoming）
        if self._db_enabled():
            self._run_async(
                self._sync_share_to_db(owner_user_id, target_user_id, sections, shared_at)
            )

    def unshare(self, owner_user_id: str, target_user_id: str) -> None:
        """取消共享"""
        # 从 owner 的 shares.json 移除
        shares = _read_json(self._shares_path(owner_user_id)) or []
        shares = [s for s in shares if s.get("target_user_id") != target_user_id]
        _atomic_write_json(self._shares_path(owner_user_id), shares)

        # 从 target 的 incoming.json 移除
        incoming = _read_json(self._incoming_path(target_user_id)) or []
        incoming = [s for s in incoming if s.get("owner_user_id") != owner_user_id]
        _atomic_write_json(self._incoming_path(target_user_id), incoming)
        # DB 双写删除（best-effort，删除 share + incoming）
        if self._db_enabled():
            self._run_async(self._delete_share_from_db(owner_user_id, target_user_id))

    def list_shared_with_me(self, user_id: str) -> list[EndingNote]:
        """列出共享给我的笔记（按 owner 的 load 实时读取，应用 sections 过滤）"""
        incoming = _read_json(self._incoming_path(user_id)) or []
        results: list[EndingNote] = []
        for item in incoming:
            owner = item.get("owner_user_id")
            sections = item.get("sections")
            if not owner:
                continue
            note = self.load(owner)
            if note is None:
                continue
            # sections 过滤：None = 全部；否则把未共享章节置 None
            if sections is not None:
                for key in (
                    "personal_info",
                    "family_relations",
                    "assets",
                    "funeral_wishes",
                    "medical_wishes",
                    "digital_legacy",
                    "messages",
                    "emergency_contacts",
                    "will_intent",
                ):
                    if key not in sections:
                        setattr(note, key, None)
            results.append(note)
        return results

    def list_my_shares(self, user_id: str) -> list[str]:
        """列出我共享给了谁（返回 target_user_id 列表）"""
        shares = _read_json(self._shares_path(user_id)) or []
        return [s.get("target_user_id") for s in shares if s.get("target_user_id")]

    # ------------------------------------------------------------------
    # 投递触发
    # ------------------------------------------------------------------

    def trigger_delivery(self, owner_user_id: str, trigger_type: str) -> dict[str, Any]:
        """触发投递

        Args:
            owner_user_id: 笔记所有者
            trigger_type: death_confirmation / date / manual

        Returns:
            {delivered: bool, recipients: [...], pending_days: int, message: str}

        notification-guardrails.md 合规：
            - death_confirmation 需要 7 天等待期（避免情绪冲动决策）
              → 首次调用时写入 pending_deliveries，状态 pending，deliver_at = now + 7d
              → 7 天内再次调用返回 pending_days（不提前投递）
              → 7 天后由外部 cron/scheduler 完成投递（不在本方法内自动执行）
            - date / manual 立即投递（仍受 NotificationGuardrail.can_send 约束，
              但本方法不主动调 can_send，留给上游决定是否真的发出推送）
        """
        if trigger_type not in ("death_confirmation", "date", "manual"):
            return {
                "delivered": False,
                "recipients": [],
                "pending_days": 0,
                "message": f"未知 trigger_type: {trigger_type}",
            }

        # 收件人 = 当前 shared_with 列表
        recipients = self.list_my_shares(owner_user_id)

        if trigger_type == "death_confirmation":
            return self._trigger_death_confirmation(owner_user_id, recipients)
        # date / manual：立即投递
        return {
            "delivered": True,
            "recipients": recipients,
            "pending_days": 0,
            "message": f"trigger_type={trigger_type} 已立即投递给 {len(recipients)} 位收件人",
            "triggered_at": datetime.now().isoformat(),
        }

    def _trigger_death_confirmation(
        self, owner_user_id: str, recipients: list[str]
    ) -> dict[str, Any]:
        """死亡确认触发的 7 天等待期处理

        状态机：
            pending  -> 等待 7 天 -> ready -> delivered
            首次调用：写 pending，deliver_at = now + 7d，返回 pending_days=7
            7 天内再次调用：返回剩余等待天数
            7 天后调用：状态 -> ready，等待外部 scheduler 真正投递
                       （不在本方法内自动发推送，遵守 notification-guardrails）
        """
        pending_path = self._pending_deliveries_path(owner_user_id)
        pending_list = _read_json(pending_path) or []
        now = datetime.now()

        # 找已有的 death_confirmation pending
        existing = next(
            (p for p in pending_list if p.get("trigger_type") == "death_confirmation"),
            None,
        )

        if existing is None:
            # 首次触发：写 pending
            deliver_at = now + timedelta(days=DEATH_CONFIRMATION_WAIT_DAYS)
            existing = {
                "trigger_type": "death_confirmation",
                "triggered_at": now.isoformat(),
                "deliver_at": deliver_at.isoformat(),
                "recipients": recipients,
                "status": "pending",
            }
            pending_list.append(existing)
            _atomic_write_json(pending_path, pending_list)
            # DB 双写（best-effort，INSERT pending delivery）
            if self._db_enabled():
                self._run_async(
                    self._sync_pending_delivery_to_db(
                        owner_user_id=owner_user_id,
                        trigger_type="death_confirmation",
                        triggered_at=now,
                        deliver_at=deliver_at,
                        recipients=recipients,
                        status="pending",
                        delivered_at=None,
                    )
                )
            return {
                "delivered": False,
                "recipients": recipients,
                "pending_days": DEATH_CONFIRMATION_WAIT_DAYS,
                "message": (
                    f"死亡确认触发已记录，需等待 {DEATH_CONFIRMATION_WAIT_DAYS} 天 "
                    "（避免情绪冲动决策）。等待期满后请再次调用本接口完成投递。"
                ),
                "triggered_at": existing["triggered_at"],
                "deliver_at": existing["deliver_at"],
            }

        # 已有 pending：检查是否到等待期
        try:
            deliver_at = datetime.fromisoformat(existing["deliver_at"])
        except (ValueError, KeyError):
            deliver_at = now + timedelta(days=DEATH_CONFIRMATION_WAIT_DAYS)

        if now < deliver_at:
            remaining = (deliver_at - now).days
            return {
                "delivered": False,
                "recipients": recipients,
                "pending_days": max(remaining, 0),
                "message": (
                    f"死亡确认触发等待期未满，还需 {remaining} 天 "
                    f"（deliver_at={deliver_at.isoformat()}）"
                ),
                "triggered_at": existing.get("triggered_at"),
                "deliver_at": existing.get("deliver_at"),
            }

        # 等待期满：状态置 ready，等待外部 scheduler 投递
        existing["status"] = "ready"
        existing["delivered_at"] = now.isoformat()
        existing["recipients"] = recipients
        _atomic_write_json(pending_path, pending_list)
        # DB 双写（best-effort，UPDATE pending delivery → ready）
        if self._db_enabled():
            try:
                triggered_at_dt = datetime.fromisoformat(existing["triggered_at"])
            except (KeyError, ValueError, TypeError):
                triggered_at_dt = now
            self._run_async(
                self._sync_pending_delivery_to_db(
                    owner_user_id=owner_user_id,
                    trigger_type="death_confirmation",
                    triggered_at=triggered_at_dt,
                    deliver_at=deliver_at,
                    recipients=recipients,
                    status="ready",
                    delivered_at=now,
                )
            )
        return {
            "delivered": True,
            "recipients": recipients,
            "pending_days": 0,
            "message": (
                "死亡确认触发等待期满，已标记为 ready；"
                "实际投递由外部 scheduler 在 NotificationGuardrail.can_send 通过后执行。"
            ),
            "triggered_at": existing.get("triggered_at"),
            "deliver_at": existing.get("deliver_at"),
        }
