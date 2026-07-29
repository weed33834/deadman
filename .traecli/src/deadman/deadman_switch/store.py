"""Dead Man Switch 存储层 - 加密存储 + 多因子状态机

存储路径：
    ~/.deadman/deadman_switch/{user_id}/switch.json   加密的 switch 主记录
    ~/.deadman/deadman_switch/{user_id}/checkins.json  check-in 日志（追加）

加密原语复用 ending_note.store（PBKDF2 + HMAC 流密码 + per-user passphrase）。
PIPL 第五章：email / phone 在 SwitchConfig 中已脱敏，存储层不再做二次脱敏。

状态机：
    ACTIVE      → SUSPECTED   连续 missed_threshold 次 check-in 失联
    SUSPECTED   → VERIFYING   紧急联系人开始确认
    VERIFYING   → CONFIRMED   所有紧急联系人确认失联 + 律师介入 + 所有继承人确认
                              + 7 天冷静期结束
    CONFIRMED   → EXECUTED    执行预设动作（actions.SwitchActionExecutor）
    任意状态     → ACTIVE      用户主动 check-in，或紧急联系人回复"安好"
    任意状态     → CANCELLED   用户主动取消

safety-protocol.md：触发死亡推定后等待期至少 7 天（cooldown_days），
                    期间可撤销（cancel / 任意阶段 check-in 即重置 ACTIVE）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..ending_note.store import (
    _atomic_write_json,
    _decrypt,
    _encrypt,
    _read_json,
)
from .models import CheckInLog, SwitchConfig, SwitchRecord, SwitchState

logger = logging.getLogger(__name__)


# deadman_switch 的 per-user passphrase 派生标签
# 与 ending_note 区分（即使全局 secret 泄露，两套数据互不串通）
_PASSPHRASE_LABEL = "deadman-switch"


def _get_switch_passphrase(user_id: str) -> bytes:
    """从全局 secret + user_id 派生 deadman_switch 专用口令

    复用 ending_note.store._get_passphrase 的全局 secret 来源（环境变量
    DEADMAN_ENDING_NOTE_PASSPHRASE 或开发默认值），但派生标签不同，
    保证 ending_note 与 deadman_switch 即使同一 user_id 也用不同密钥。
    """
    global_secret = os.environ.get(
        "DEADMAN_ENDING_NOTE_PASSPHRASE",
        "deadman-ending-note-dev-passphrase",
    )
    import hashlib
    import hmac

    return hmac.new(
        global_secret.encode("utf-8"),
        (_PASSPHRASE_LABEL + ":" + user_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()


class SwitchStore:
    """Dead Man Switch 存储 + 状态机推进

    所有写入操作原子化（先写 .tmp 再 os.replace）；
    所有读取失败返回 None，不抛异常。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        if data_dir is None:
            # 优先读 DEADMAN_SWITCH_DATA_DIR 环境变量（与 .env.example / config.py 对齐）
            env_dir = os.getenv("DEADMAN_SWITCH_DATA_DIR")
            data_dir = Path(env_dir) if env_dir else Path.home() / ".deadman" / "deadman_switch"
        self.data_dir: Path = Path(data_dir)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("SwitchStore 创建数据目录失败 %s: %s", self.data_dir, exc)

    # ==================================================================
    # 路径辅助
    # ==================================================================
    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / user_id

    def _switch_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "switch.json"

    def _checkins_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "checkins.json"

    # ==================================================================
    # 加密 / 解密
    # ==================================================================
    def _encrypt_record(self, record: SwitchRecord) -> dict[str, Any]:
        """加密 SwitchRecord -> envelope dict（落盘前）"""
        plaintext = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")
        passphrase = _get_switch_passphrase(record.user_id)
        return _encrypt(plaintext, passphrase)

    def _decrypt_record(self, envelope: dict[str, Any], user_id: str) -> SwitchRecord | None:
        """解密 envelope dict -> SwitchRecord"""
        try:
            passphrase = _get_switch_passphrase(user_id)
            plaintext = _decrypt(envelope, passphrase)
        except ValueError as exc:
            logger.warning("解密 switch 失败 user=%s: %s", user_id, exc)
            return None
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("解析 switch JSON 失败 user=%s: %s", user_id, exc)
            return None
        return SwitchRecord.from_dict(data)

    # ==================================================================
    # CRUD
    # ==================================================================
    def init_switch(
        self,
        user_id: str,
        config: SwitchConfig | None = None,
    ) -> SwitchRecord:
        """初始化 switch（如果已存在则覆盖配置但保留 state 历史）"""
        existing = self.load(user_id)
        if existing is not None:
            # 已存在：更新 config，重置 contact_confirmations 等
            existing.config = config or SwitchConfig()
            existing.contact_confirmations = {}
            existing.contact_confirmed_at = {}
            existing.heir_confirmations = {}
            existing.heir_confirmed_at = {}
            existing.lawyer_engaged = False
            existing.lawyer_engaged_at = None
            self.save(existing)
            return existing
        record = SwitchRecord.new(user_id, config)
        self.save(record)
        return record

    def save(self, record: SwitchRecord) -> None:
        """保存 switch 记录（加密 + 原子写入）"""
        envelope = self._encrypt_record(record)
        _atomic_write_json(self._switch_path(record.user_id), envelope)

    def load(self, user_id: str) -> SwitchRecord | None:
        """加载 switch 记录；不存在返回 None"""
        envelope = _read_json(self._switch_path(user_id))
        if envelope is None:
            return None
        return self._decrypt_record(envelope, user_id)

    def list_all_users(self) -> list[str]:
        """列出所有已初始化 switch 的 user_id（扫描 data_dir 子目录）

        供 SwitchAutoTicker 后台调度器遍历所有用户调用 tick()。
        """
        try:
            return sorted(
                d.name for d in self.data_dir.iterdir()
                if d.is_dir() and (d / "switch.json").exists()
            )
        except OSError:
            return []

    def delete(self, user_id: str) -> bool:
        """删除 switch 记录及 checkins 日志"""
        deleted = False
        for path in (self._switch_path(user_id), self._checkins_path(user_id)):
            if path.exists():
                try:
                    path.unlink()
                    deleted = True
                except OSError as exc:
                    logger.warning("删除文件失败 %s: %s", path, exc)
        user_dir = self._user_dir(user_id)
        try:
            if user_dir.exists() and not any(user_dir.iterdir()):
                user_dir.rmdir()
        except OSError:
            pass
        return deleted

    # ==================================================================
    # Check-in 日志
    # ==================================================================
    def record_check_in(self, user_id: str, method: str = "web") -> SwitchRecord | None:
        """记录一次 check-in，立即把状态机重置回 ACTIVE

        任意阶段（包括 CONFIRMED 冷静期内）用户主动 check-in
        都视为"还活着"，立即重置 ACTIVE。
        """
        record = self.load(user_id)
        if record is None:
            return None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        record.last_check_in = now
        record.last_missed = None
        record.missed_count = 0
        # 重置多因子验证状态
        record.contact_confirmations = {}
        record.contact_confirmed_at = {}
        record.heir_confirmations = {}
        record.heir_confirmed_at = {}
        record.lawyer_engaged = False
        record.lawyer_engaged_at = None
        record.confirmed_at = None
        # 仅在非 ACTIVE 状态下记录状态转换
        if record.state != SwitchState.ACTIVE:
            old = record.state
            record.state = SwitchState.ACTIVE
            record.state_history.append(
                {
                    "state": SwitchState.ACTIVE.value,
                    "timestamp": now.isoformat(),
                    "reason": f"user_check_in_reset_from_{old.value}",
                }
            )
        # 追加 check-in 日志
        log = CheckInLog(user_id=user_id, check_in_at=now, method=method)
        logs: list[dict[str, Any]] = _read_json(self._checkins_path(user_id)) or []
        logs.append(log.to_dict())
        # 限制日志大小（保留最近 200 条）
        if len(logs) > 200:
            logs = logs[-200:]
        _atomic_write_json(self._checkins_path(user_id), logs)
        self.save(record)
        return record

    def list_check_ins(self, user_id: str, limit: int = 50) -> list[CheckInLog]:
        """列出 check-in 日志（按时间倒序，最多 limit 条）"""
        logs_data: list[dict[str, Any]] = _read_json(self._checkins_path(user_id)) or []
        logs = [CheckInLog.from_dict(d) for d in logs_data if isinstance(d, dict)]
        logs.sort(key=lambda x: x.check_in_at, reverse=True)
        return logs[:limit]

    # ==================================================================
    # 状态机推进
    # ==================================================================
    def tick(self, user_id: str, now: datetime | None = None) -> SwitchRecord | None:
        """检查是否需要状态转换（基于 last_check_in 与 config）

        - ACTIVE → SUSPECTED：now - last_check_in > missed_threshold * check_in_frequency_days
        - SUSPECTED → VERIFYING：自动推进（紧急联系人开始确认）
        - VERIFYING → CONFIRMED：所有紧急联系人 + 所有继承人确认 + 律师介入
                                 （由 verify_emergency_contact / verify_heir 推进，
                                  tick 不主动推进此步）
        - CONFIRMED → EXECUTED：冷静期结束（cooldown_days 后）
                                此处仅检查可执行性；实际执行由
                                SwitchActionExecutor.execute_confirmed 调用

        Returns:
            更新后的 SwitchRecord；如不需要转换则返回原状 record
        """
        record = self.load(user_id)
        if record is None:
            return None
        if record.state in (SwitchState.EXECUTED, SwitchState.CANCELLED):
            return record

        now_dt = now or datetime.now(timezone.utc).replace(tzinfo=None)
        cfg = record.config
        # 阈值时间：连续 missed_threshold 次 check_in_frequency_days 未 check-in
        threshold = timedelta(
            days=cfg.check_in_frequency_days * max(cfg.missed_threshold, 1)
        )
        last_check = record.last_check_in or record.created_at
        elapsed = now_dt - last_check

        # ACTIVE → SUSPECTED
        if record.state == SwitchState.ACTIVE:
            if elapsed > threshold:
                self.transition_to(
                    user_id,
                    SwitchState.SUSPECTED,
                    reason=(
                        f"missed_{cfg.missed_threshold}_checkins_"
                        f"elapsed_days={elapsed.days}"
                    ),
                    now=now_dt,
                )
                record = self.load(user_id)
                if record is None:
                    return None
            else:
                return record

        # SUSPECTED → VERIFYING：发邮件 + 短信询问
        # 在 tick 内自动推进（不需要外部确认），写入历史
        if record.state == SwitchState.SUSPECTED:
            self.transition_to(
                user_id,
                SwitchState.VERIFYING,
                reason="verification_started_emailed_and_sms",
                now=now_dt,
            )
            record = self.load(user_id)
            if record is None:
                return None

        # VERIFYING → CONFIRMED：所有联系人 + 所有继承人确认 + 律师介入
        # 这里检查是否满足推进条件；若未满足则停留
        if record.state == SwitchState.VERIFYING:
            ok, reason = self._check_verification_complete(record)
            if ok:
                self.transition_to(
                    user_id,
                    SwitchState.CONFIRMED,
                    reason=reason,
                    now=now_dt,
                )
                record = self.load(user_id)
                if record is None:
                    return None
            else:
                return record

        # CONFIRMED → EXECUTED：冷静期结束（仅检查可执行性；实际执行由外部触发）
        # tick 不主动 EXECUTED，仅返回 record；调用方可根据
        # is_cooldown_passed() 决定是否调 SwitchActionExecutor.execute_confirmed
        return record

    def _check_verification_complete(self, record: SwitchRecord) -> tuple[bool, str]:
        """检查 VERIFYING → CONFIRMED 的所有前置条件

        条件：
            1. emergency_contacts 不为空
            2. 每个紧急联系人都确认 "失联"（contact_confirmations[cid] == True）
            3. lawyer_user_id 已设置且 lawyer_engaged == True
            4. heir_user_ids 不为空
            5. 每个继承人都确认 "失联"（heir_confirmations[hid] == True）

        Returns:
            (ok, reason) - reason 在 ok=True 时为推进原因，ok=False 时为阻塞原因
        """
        cfg = record.config
        if not cfg.emergency_contacts:
            return False, "missing_emergency_contacts"
        for cid in cfg.emergency_contacts:
            if not record.contact_confirmations.get(cid, False):
                return False, f"contact_{cid}_not_confirmed"
        if cfg.lawyer_user_id and not record.lawyer_engaged:
            return False, "lawyer_not_engaged"
        if not cfg.heir_user_ids:
            return False, "missing_heirs"
        for hid in cfg.heir_user_ids:
            if not record.heir_confirmations.get(hid, False):
                return False, f"heir_{hid}_not_confirmed"
        return True, "all_verifications_complete"

    # ==================================================================
    # 状态转换
    # ==================================================================
    def transition_to(
        self,
        user_id: str,
        new_state: SwitchState,
        reason: str,
        now: datetime | None = None,
    ) -> SwitchRecord | None:
        """显式状态转换 + 记录 history

        不做合法性校验（调用方负责）；仅记录状态变化与时间戳。
        """
        record = self.load(user_id)
        if record is None:
            return None
        now_dt = now or datetime.now(timezone.utc).replace(tzinfo=None)
        old_state = record.state
        record.state = new_state
        # CONFIRMED 状态进入时记录 confirmed_at（冷静期起算）
        if new_state == SwitchState.CONFIRMED and record.confirmed_at is None:
            record.confirmed_at = now_dt
        record.state_history.append(
            {
                "state": new_state.value,
                "timestamp": now_dt.isoformat(),
                "reason": reason,
                "from": old_state.value,
            }
        )
        self.save(record)
        return record

    # ==================================================================
    # 多因子验证
    # ==================================================================
    def verify_emergency_contact(
        self,
        user_id: str,
        contact_user_id: str,
        confirmed: bool,
    ) -> tuple[SwitchRecord | None, str]:
        """紧急联系人确认 / 否认失联

        Args:
            contact_user_id: 紧急联系人 user_id（必须在 config.emergency_contacts 中）
            confirmed: True=该联系人确认当事人失联；False=该联系人表示当事人安好

        Returns:
            (record, message)
            - confirmed=False 时立即把状态机重置回 ACTIVE（"安好" 反馈）
            - confirmed=True 时记录该联系人确认失联
        """
        record = self.load(user_id)
        if record is None:
            return None, "switch_not_initialized"
        if contact_user_id not in record.config.emergency_contacts:
            return record, "contact_not_in_emergency_list"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if not confirmed:
            # 联系人表示当事人安好 → 立即重置 ACTIVE
            if record.state != SwitchState.ACTIVE:
                old = record.state
                record.state = SwitchState.ACTIVE
                record.state_history.append(
                    {
                        "state": SwitchState.ACTIVE.value,
                        "timestamp": now.isoformat(),
                        "reason": (
                            f"emergency_contact_{contact_user_id}_"
                            f"reports_alive_from_{old.value}"
                        ),
                    }
                )
            record.contact_confirmations = {}
            record.contact_confirmed_at = {}
            record.heir_confirmations = {}
            record.heir_confirmed_at = {}
            record.lawyer_engaged = False
            record.lawyer_engaged_at = None
            record.confirmed_at = None
            record.last_check_in = now
            record.missed_count = 0
            record.last_missed = None
            self.save(record)
            return record, "reset_to_active_due_to_alive_report"
        # confirmed=True：记录该联系人确认失联
        record.contact_confirmations[contact_user_id] = True
        record.contact_confirmed_at[contact_user_id] = now.isoformat()
        self.save(record)
        return record, "contact_confirmed_missing"

    def verify_heir(
        self,
        user_id: str,
        heir_user_id: str,
        confirmed: bool,
    ) -> tuple[SwitchRecord | None, str]:
        """法定继承人确认 / 否认失联（逻辑同 verify_emergency_contact）"""
        record = self.load(user_id)
        if record is None:
            return None, "switch_not_initialized"
        if heir_user_id not in record.config.heir_user_ids:
            return record, "heir_not_in_heir_list"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if not confirmed:
            # 继承人表示当事人安好 → 立即重置 ACTIVE
            if record.state != SwitchState.ACTIVE:
                old = record.state
                record.state = SwitchState.ACTIVE
                record.state_history.append(
                    {
                        "state": SwitchState.ACTIVE.value,
                        "timestamp": now.isoformat(),
                        "reason": (
                            f"heir_{heir_user_id}_"
                            f"reports_alive_from_{old.value}"
                        ),
                    }
                )
            record.contact_confirmations = {}
            record.contact_confirmed_at = {}
            record.heir_confirmations = {}
            record.heir_confirmed_at = {}
            record.lawyer_engaged = False
            record.lawyer_engaged_at = None
            record.confirmed_at = None
            record.last_check_in = now
            record.missed_count = 0
            record.last_missed = None
            self.save(record)
            return record, "reset_to_active_due_to_alive_report"
        record.heir_confirmations[heir_user_id] = True
        record.heir_confirmed_at[heir_user_id] = now.isoformat()
        self.save(record)
        return record, "heir_confirmed_missing"

    def engage_lawyer(self, user_id: str) -> tuple[SwitchRecord | None, str]:
        """律师介入标记（必须在 VERIFYING 状态、且 config.lawyer_user_id 已设置）"""
        record = self.load(user_id)
        if record is None:
            return None, "switch_not_initialized"
        if record.state not in (SwitchState.VERIFYING, SwitchState.CONFIRMED):
            return record, "not_in_verifying_or_confirmed_state"
        if not record.config.lawyer_user_id:
            return record, "no_lawyer_configured"
        record.lawyer_engaged = True
        record.lawyer_engaged_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        self.save(record)
        return record, "lawyer_engaged"

    # ==================================================================
    # 取消
    # ==================================================================
    def cancel(self, user_id: str, reason: str = "user_cancelled") -> SwitchRecord | None:
        """用户主动取消 switch（无论处于哪个阶段）"""
        record = self.load(user_id)
        if record is None:
            return None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old_state = record.state
        record.state = SwitchState.CANCELLED
        record.state_history.append(
            {
                "state": SwitchState.CANCELLED.value,
                "timestamp": now.isoformat(),
                "reason": reason,
                "from": old_state.value,
            }
        )
        self.save(record)
        return record

    # ==================================================================
    # 冷静期查询
    # ==================================================================
    def is_cooldown_passed(self, user_id: str, now: datetime | None = None) -> bool:
        """CONFIRMED 状态下冷静期是否已过（cooldown_days 后才能执行）"""
        record = self.load(user_id)
        if record is None:
            return False
        if record.state != SwitchState.CONFIRMED:
            return False
        if record.confirmed_at is None:
            return False
        now_dt = now or datetime.now(timezone.utc).replace(tzinfo=None)
        return (now_dt - record.confirmed_at) >= timedelta(days=record.config.cooldown_days)

    def cooldown_remaining_days(self, user_id: str, now: datetime | None = None) -> int:
        """剩余冷静期天数（>=0；非 CONFIRMED 状态返回 0）"""
        record = self.load(user_id)
        if record is None:
            return 0
        if record.state != SwitchState.CONFIRMED or record.confirmed_at is None:
            return 0
        now_dt = now or datetime.now(timezone.utc).replace(tzinfo=None)
        elapsed = now_dt - record.confirmed_at
        total = timedelta(days=record.config.cooldown_days)
        remaining = total - elapsed
        if remaining <= timedelta(0):
            return 0
        return remaining.days + (1 if remaining.seconds > 0 else 0)

    # ==================================================================
    # 待执行动作管理
    # ==================================================================
    def add_pending_action(self, user_id: str, action: str) -> None:
        """追加一个待执行动作（CONFIRMED → EXECUTED 阶段）"""
        record = self.load(user_id)
        if record is None:
            return
        if action not in record.pending_actions:
            record.pending_actions.append(action)
        self.save(record)

    def mark_action_executed(
        self,
        user_id: str,
        action: str,
        result: dict[str, Any],
    ) -> None:
        """标记一个动作已执行（从 pending_actions 移除，写入 executed_actions）"""
        record = self.load(user_id)
        if record is None:
            return
        if action in record.pending_actions:
            record.pending_actions.remove(action)
        record.executed_actions.append(
            {
                "action": action,
                "executed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "result": result,
            }
        )
        # 所有动作执行完毕 → 状态转 EXECUTED
        if not record.pending_actions and record.state == SwitchState.CONFIRMED:
            old = record.state
            record.state = SwitchState.EXECUTED
            record.state_history.append(
                {
                    "state": SwitchState.EXECUTED.value,
                    "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    "reason": "all_actions_executed",
                    "from": old.value,
                }
            )
        self.save(record)
