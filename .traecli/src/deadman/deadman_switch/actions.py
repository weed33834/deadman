"""Dead Man Switch 动作执行器 - CONFIRMED → EXECUTED 阶段

执行动作清单：
    - deliver_ending_note   发送身后信件（调 ending_note.trigger_delivery）
    - trigger_vault_on_death  通知数字遗产保险库（调 vault.trigger_delivery on_death）
    - notify_lawyer         通知律师（记录待办，不自动发送邮件）
    - notify_heirs          通知法定继承人（记录待办）

每个动作都过 NotificationGuardrail.can_send() 检查：
    - 通过 → 执行 + 记录 sent_log
    - 不通过 → 失败动作记入 pending_actions 等待重试
    - 安全降级：失败仅 warning，不阻塞其他动作

safety-protocol.md：触发死亡推定后等待期至少 7 天；
    execute_confirmed 必须先检查 is_cooldown_passed，
    否则抛 RuntimeError 拒绝执行。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..notification.guardrail import NotificationGuardrail
from .models import SwitchRecord, SwitchState
from .store import SwitchStore

logger = logging.getLogger(__name__)


# 动作清单（与 SwitchRecord.pending_actions 字段对应）
ACTION_DELIVER_ENDING_NOTE = "deliver_ending_note"
ACTION_TRIGGER_VAULT_ON_DEATH = "trigger_vault_on_death"
ACTION_NOTIFY_LAWYER = "notify_lawyer"
ACTION_NOTIFY_HEIRS = "notify_heirs"

DEFAULT_ACTIONS: list[str] = [
    ACTION_DELIVER_ENDING_NOTE,
    ACTION_TRIGGER_VAULT_ON_DEATH,
    ACTION_NOTIFY_LAWYER,
    ACTION_NOTIFY_HEIRS,
]


class SwitchActionExecutor:
    """死亡推定确认后的动作执行器

    设计原则：
        - 不可逆操作（执行遗嘱/关闭账户/发送身后信件）必须二次确认
          （此处二次确认 = CONFIRMED 状态进入需要所有继承人二次确认 + 7 天冷静期；
           execute_confirmed 在冷静期未过时抛 RuntimeError 拒绝执行）
        - 每个动作独立 try/except，单个失败不影响其他
        - 失败的动作记入 pending_actions 等待重试
        - 所有主动通知过 NotificationGuardrail.can_send() 检查
    """

    def __init__(
        self,
        store: SwitchStore | None = None,
        guardrail: NotificationGuardrail | None = None,
    ) -> None:
        self.store = store or SwitchStore()
        # guardrail 默认复用 ~/.deadman/notifications/
        self.guardrail = guardrail or NotificationGuardrail()

    # ==================================================================
    # 二次确认检查
    # ==================================================================
    def _assert_cooldown_passed(self, user_id: str) -> None:
        """断言冷静期已过；否则抛 RuntimeError 拒绝执行

        safety-protocol.md：触发死亡推定后等待期至少 7 天
        """
        if not self.store.is_cooldown_passed(user_id):
            remaining = self.store.cooldown_remaining_days(user_id)
            raise RuntimeError(
                f"deadman_switch cooldown not passed for user={user_id}; "
                f"remaining_days={remaining}; "
                "execution refused during the mandatory reflection period"
            )

    def _assert_state_confirmed(self, record: SwitchRecord) -> None:
        """断言 record 处于 CONFIRMED 状态；否则抛 RuntimeError"""
        if record.state != SwitchState.CONFIRMED:
            raise RuntimeError(
                f"deadman_switch state is {record.state.value}, "
                "must be CONFIRMED to execute actions"
            )

    # ==================================================================
    # 主入口：执行所有动作
    # ==================================================================
    def execute_confirmed(self, user_id: str) -> dict[str, Any]:
        """执行死亡推定确认后的全部动作

        Returns:
            {
                "executed": list[str],        成功的动作
                "failed": list[dict],          失败的动作 + 原因
                "pending": list[str],          仍待执行（下次重试）
                "state": str,                  执行后状态
            }

        Raises:
            RuntimeError: 状态非 CONFIRMED / 冷静期未过
        """
        # 先检查状态：必须 CONFIRMED 才能执行
        record = self.store.load(user_id)
        if record is None:
            raise RuntimeError(f"switch record not found: {user_id}")
        self._assert_state_confirmed(record)
        # 再检查冷静期：safety-protocol.md 要求至少 7 天
        self._assert_cooldown_passed(user_id)

        # 确保有 pending_actions；若无则用默认清单
        if not record.pending_actions:
            for action in DEFAULT_ACTIONS:
                self.store.add_pending_action(user_id, action)
            record = self.store.load(user_id)
            if record is None:
                raise RuntimeError(f"switch record reload failed: {user_id}")

        executed: list[str] = []
        failed: list[dict[str, Any]] = []
        # 复制一份 pending_actions 防止遍历中变更
        pending_snapshot = list(record.pending_actions)

        for action in pending_snapshot:
            try:
                result = self._dispatch_action(action, record)
                self.store.mark_action_executed(user_id, action, result)
                executed.append(action)
            except _ActionRetryableError as exc:
                # 可重试的失败：保留在 pending_actions 中等待下次执行
                failed.append({"action": action, "reason": str(exc), "retryable": True})
                logger.warning(
                    "deadman_switch action %s failed (retryable) user=%s: %s",
                    action,
                    user_id,
                    exc,
                )
            except Exception as exc:
                # 不可重试的失败：从 pending 移除（避免反复触发）
                # 但记录在 executed_actions 中标记为 failed
                failed.append({"action": action, "reason": str(exc), "retryable": False})
                self.store.mark_action_executed(
                    user_id,
                    action,
                    {"success": False, "error": str(exc), "non_retryable": True},
                )
                logger.warning(
                    "deadman_switch action %s failed (non-retryable) user=%s: %s",
                    action,
                    user_id,
                    exc,
                )

        # 重新加载获取最终状态
        final = self.store.load(user_id)
        state = final.state.value if final else "UNKNOWN"
        still_pending = final.pending_actions if final else []
        return {
            "executed": executed,
            "failed": failed,
            "pending": list(still_pending),
            "state": state,
        }

    # ==================================================================
    # 动作分发
    # ==================================================================
    def _dispatch_action(self, action: str, record: SwitchRecord) -> dict[str, Any]:
        """根据 action 名分发到具体执行器

        Raises:
            _ActionRetryableError: 可重试失败（NotificationGuardrail 拒绝、
                                    ending_note / vault 暂时不可用等）
            ValueError: 未知 action 名（不可重试）
        """
        if action == ACTION_DELIVER_ENDING_NOTE:
            return self._do_deliver_ending_note(record)
        if action == ACTION_TRIGGER_VAULT_ON_DEATH:
            return self._do_trigger_vault_on_death(record)
        if action == ACTION_NOTIFY_LAWYER:
            return self._do_notify_lawyer(record)
        if action == ACTION_NOTIFY_HEIRS:
            return self._do_notify_heirs(record)
        raise ValueError(f"unknown action: {action}")

    # ==================================================================
    # 单个动作实现
    # ==================================================================
    def _do_deliver_ending_note(self, record: SwitchRecord) -> dict[str, Any]:
        """发送身后信件 - 调 ending_note.trigger_delivery

        notification-guardrails.md：所有主动通知过 can_send() 检查
        """
        now = datetime.now()
        # 对每个收件人（紧急联系人 + 继承人）检查 can_send
        # 失败的收件人作为 retryable 失败
        all_recipients = list(
            set(record.config.emergency_contacts) | set(record.config.heir_user_ids)
        )
        allowed_recipients: list[str] = []
        blocked_recipients: list[str] = []
        for rid in all_recipients:
            ok, reason = self.guardrail.can_send(rid, now)
            if ok:
                allowed_recipients.append(rid)
            else:
                blocked_recipients.append(f"{rid}:{reason}")

        # 调用 ending_note.trigger_delivery（death_confirmation trigger）
        try:
            from ..ending_note.store import EndingNoteStore

            ending_store = EndingNoteStore()
            result = ending_store.trigger_delivery(
                record.user_id, "death_confirmation"
            )
        except Exception as exc:
            # 系统级失败 → 可重试
            raise _ActionRetryableError(
                f"ending_note trigger_delivery failed: {exc}"
            ) from exc

        # 若所有收件人被 NotificationGuardrail 拒绝 → 可重试
        if not allowed_recipients and all_recipients:
            raise _ActionRetryableError(
                "all recipients blocked by NotificationGuardrail; "
                f"blocked={blocked_recipients}"
            )

        # 记录 sent_log（仅对允许的收件人）
        for rid in allowed_recipients:
            self.guardrail.record_send(
                rid,
                content="[脱敏] deadman_switch 身后信件投递通知",
                channel="deadman_switch",
            )
        return {
            "success": True,
            "delivered_recipients": allowed_recipients,
            "blocked_recipients": blocked_recipients,
            "ending_note_result": {
                k: v for k, v in result.items() if k != "content"
            },
        }

    def _do_trigger_vault_on_death(self, record: SwitchRecord) -> dict[str, Any]:
        """通知数字遗产保险库 - 调 vault.trigger_delivery on_death

        vault 自身有 7 天等待期 + 受益人二次确认（独立机制），
        此处仅触发，不直接投递。
        """
        try:
            from ..vault.store import VaultStore, TRIGGER_ON_DEATH

            vault_store = VaultStore()
            # 列出 owner 所有条目，对每个 on_death 触发的条目调 trigger_delivery
            items = vault_store.list_items(record.user_id, record.user_id)
            triggered: list[dict[str, Any]] = []
            for item_meta in items:
                if item_meta.get("delivery_trigger") != TRIGGER_ON_DEATH:
                    continue
                item_id = item_meta.get("item_id", "")
                result = vault_store.trigger_delivery(
                    item_id, TRIGGER_ON_DEATH, record.user_id
                )
                triggered.append(
                    {
                        "item_id": item_id,
                        "delivered": result.get("delivered", False),
                        "pending_days": result.get("pending_days", 0),
                        "reason": result.get("reason", ""),
                    }
                )
            return {
                "success": True,
                "triggered_count": len(triggered),
                "items": triggered,
            }
        except Exception as exc:
            raise _ActionRetryableError(
                f"vault trigger_delivery failed: {exc}"
            ) from exc

    def _do_notify_lawyer(self, record: SwitchRecord) -> dict[str, Any]:
        """通知律师 - 仅记录待办，不自动发送邮件

        notification-guardrails.md：律师通知不通过自动渠道推送；
        律师通常在继承人主动联系时由人工 / 律师事务所渠道通知，
        本系统仅记录"已通知律师"事实与待办清单。
        """
        lawyer_id = record.config.lawyer_user_id
        if not lawyer_id:
            # 无律师配置：标记为已跳过（非失败）
            return {
                "success": True,
                "skipped": True,
                "reason": "no_lawyer_configured",
            }
        # 记录到 executed_actions 表明"已通知律师"事实
        # 不调用 NotificationGuardrail.can_send / record_send：
        # 因为律师通知由继承人在冷静期结束后主动发起（人工渠道），
        # 系统不主动推送给律师（避免对律师造成骚扰）。
        return {
            "success": True,
            "lawyer_user_id": lawyer_id,
            "notified_via": "manual_todo",
            "note": (
                "律师通知待办已记录；实际通知由法定继承人在冷静期结束后"
                "主动发起（人工渠道），系统不自动推送"
            ),
        }

    def _do_notify_heirs(self, record: SwitchRecord) -> dict[str, Any]:
        """通知法定继承人 - 走 NotificationGuardrail 检查

        每个继承人独立检查 can_send；通过则 record_send，
        不通过则作为 retryable 失败（保留在 pending_actions）。
        """
        now = datetime.now()
        heirs = record.config.heir_user_ids
        if not heirs:
            return {"success": True, "skipped": True, "reason": "no_heirs_configured"}
        notified: list[str] = []
        blocked: list[str] = []
        for hid in heirs:
            ok, reason = self.guardrail.can_send(hid, now)
            if ok:
                self.guardrail.record_send(
                    hid,
                    content="[脱敏] deadman_switch 继承人通知：当事人失联确认完成",
                    channel="deadman_switch",
                )
                notified.append(hid)
            else:
                blocked.append(f"{hid}:{reason}")
        if notified and not blocked:
            return {
                "success": True,
                "notified_heirs": notified,
            }
        if notified and blocked:
            # 部分成功：标 success=True，但保留 blocked 列表
            return {
                "success": True,
                "notified_heirs": notified,
                "blocked_heirs": blocked,
            }
        # 全部被 guardrail 拒绝 → 可重试
        raise _ActionRetryableError(
            f"all heirs blocked by NotificationGuardrail; blocked={blocked}"
        )


class _ActionRetryableError(Exception):
    """可重试动作失败 - 动作保留在 pending_actions 等待下次执行"""
    pass
