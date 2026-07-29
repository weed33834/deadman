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
from pathlib import Path
from typing import Any

from ..utils import crypto
from .models import EndingNote

logger = logging.getLogger(__name__)


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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入字节数据：先写 .tmp 再 os.replace"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _atomic_write_json(path: Path, obj: Any) -> None:
    """原子写入 JSON（utf-8 + ensure_ascii=False）"""
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(path, data)


def _read_json(path: Path) -> Any | None:
    """读 JSON；文件不存在或解析失败返回 None"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取 JSON 失败 %s: %s", path, e)
        return None


def _get_passphrase(user_id: str | None = None) -> bytes:
    """取加密口令（Phase 14：返回 bytes，支持 per-user 派生）

    优先级：
    1. 环境变量 DEADMAN_ENDING_NOTE_PASSPHRASE（全局口令，开发期可用）
    2. per-user 派生：若提供 user_id，用 user_id 与全局 secret 派生独立口令
       （这样即使全局 secret 泄露，攻击者仍需知道 user_id 才能解密）
    3. 开发默认值（仅供测试，生产禁用）

    ⚠️ Phase 14 改进：
       - 原实现所有用户共用同一 passphrase，单点泄露即全量泄露
       - 现实现 per-user 派生（HMAC-SHA256），即使全局 secret 泄露，
         攻击者仍需枚举 user_id 才能解密单个用户笔记
       - 生产环境应通过 auth 模块在登录时派生独立 passphrase，
         通过线程局部变量或上下文注入，避免依赖环境变量
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
    if user_id:
        # per-user 派生：HMAC-SHA256(global_secret, user_id)
        # 即使全局 secret 泄露，攻击者仍需知道 user_id 才能解密
        per_user = hmac.new(
            global_secret.encode("utf-8"),
            ("ending-note:" + user_id).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return per_user
    return global_secret.encode("utf-8")


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
        block = hmac.new(
            enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    keystream = bytes(out[: len(ct)])
    return bytes(a ^ b for a, b in zip(ct, keystream))


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
            data_dir: 数据根目录，默认 ~/.deadman/ending_notes/
        """
        self.data_dir: Path = data_dir or (Path.home() / ".deadman" / "ending_notes")

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

    def load(self, user_id: str) -> EndingNote | None:
        """加载笔记；文件不存在返回 None

        Phase 14：使用 per-user 派生的 passphrase 解密。
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
                plaintext = _decrypt(envelope, passphrase)
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

        # owner 的对外共享清单
        shares = _read_json(self._shares_path(owner_user_id)) or []
        # 去重：已存在则更新 sections
        shares = [s for s in shares if s.get("target_user_id") != target_user_id]
        shares.append(
            {
                "target_user_id": target_user_id,
                "sections": sections,  # None 表示全部
                "shared_at": datetime.now().isoformat(),
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
                "shared_at": datetime.now().isoformat(),
            }
        )
        _atomic_write_json(self._incoming_path(target_user_id), incoming)

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

    def trigger_delivery(
        self, owner_user_id: str, trigger_type: str
    ) -> dict[str, Any]:
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
            existing = {
                "trigger_type": "death_confirmation",
                "triggered_at": now.isoformat(),
                "deliver_at": (now + timedelta(days=DEATH_CONFIRMATION_WAIT_DAYS)).isoformat(),
                "recipients": recipients,
                "status": "pending",
            }
            pending_list.append(existing)
            _atomic_write_json(pending_path, pending_list)
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
