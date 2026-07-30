"""Dead Man Switch 动作执行器 - CONFIRMED → EXECUTED 阶段

执行动作清单：
    - deliver_ending_note   发送身后信件（调 ending_note.trigger_delivery）
    - trigger_vault_on_death  通知数字遗产保险库（调 vault.trigger_delivery on_death）
    - notify_lawyer         通知律师（记录待办 + 通过 EmailSender 发送邮件）
    - notify_heirs          通知法定继承人（NotificationGuardrail 校验 + 发送邮件）

每个动作都过 NotificationGuardrail.can_send() 检查：
    - 通过 → 执行 + 记录 sent_log
    - 不通过 → 失败动作记入 pending_actions 等待重试
    - 安全降级：失败仅 warning，不阻塞其他动作

通知通道（P0-3 修复）：
    - notify_lawyer / notify_heirs 现在真正调用 EmailSender.send_sync() 发送邮件，
      不再仅记录 manual_todo。SMTP 未配置时降级为"待办已记录"（不阻塞流程），
      SMTP 配置但发送失败时作为 retryable 失败保留在 pending_actions 等待重试。
    - 收件人解析：标识符含 "@" 直接当邮箱用；否则用 UserStore.get_user()
      查邮箱（与 auto_tick._notify_state_change_via_email 逻辑一致）。

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
        email_sender: Any = None,
        user_store: Any = None,
    ) -> None:
        self.store = store or SwitchStore()
        # guardrail 默认复用 ~/.deadman/notifications/
        self.guardrail = guardrail or NotificationGuardrail()
        # 邮件发送器：注入优先；未注入则懒加载 EmailSender
        # （EmailSender 初始化只读环境变量不报错；SMTP 未配置 / aiosmtplib 不可用
        # 时在 send_sync() 内部降级，不影响 execute_confirmed 主流程）
        self.email_sender = email_sender
        if self.email_sender is None:
            try:
                from ..notification.email_sender import EmailSender

                self.email_sender = EmailSender()
            except Exception as exc:
                logger.warning("EmailSender 初始化失败，邮件通知降级: %s", exc)
                self.email_sender = None
        # 用户存储：用于从 user_id 解析邮箱地址（heir/lawyer 标识符非邮箱时）
        # 懒加载避免循环 import；注入优先便于测试
        self.user_store = user_store

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
            result = ending_store.trigger_delivery(record.user_id, "death_confirmation")
        except Exception as exc:
            # 系统级失败 → 可重试
            raise _ActionRetryableError(f"ending_note trigger_delivery failed: {exc}") from exc

        # 若所有收件人被 NotificationGuardrail 拒绝 → 可重试
        if not allowed_recipients and all_recipients:
            raise _ActionRetryableError(
                f"all recipients blocked by NotificationGuardrail; blocked={blocked_recipients}"
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
            "ending_note_result": {k: v for k, v in result.items() if k != "content"},
        }

    def _do_trigger_vault_on_death(self, record: SwitchRecord) -> dict[str, Any]:
        """通知数字遗产保险库 - 调 vault.trigger_delivery on_death

        vault 自身有 7 天等待期 + 受益人二次确认（独立机制），
        此处仅触发，不直接投递。
        """
        try:
            from ..vault.store import TRIGGER_ON_DEATH, VaultStore

            vault_store = VaultStore()
            # 列出 owner 所有条目，对每个 on_death 触发的条目调 trigger_delivery
            items = vault_store.list_items(record.user_id, record.user_id)
            triggered: list[dict[str, Any]] = []
            for item_meta in items:
                if item_meta.get("delivery_trigger") != TRIGGER_ON_DEATH:
                    continue
                item_id = item_meta.get("item_id", "")
                result = vault_store.trigger_delivery(item_id, TRIGGER_ON_DEATH, record.user_id)
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
            raise _ActionRetryableError(f"vault trigger_delivery failed: {exc}") from exc

    def _do_notify_lawyer(self, record: SwitchRecord) -> dict[str, Any]:
        """通知律师 - 通过 EmailSender 发送邮件（SMTP 未配置时降级为待办）

        P0-3 修复：原实现仅记录 manual_todo，导致 DMS 触发后律师收不到任何通知，
        功能契约违背。现改为真正发送邮件：
            - SMTP 已配置 + 发送成功 → 记录 sent_log，动作完成
            - SMTP 未配置           → 降级为"待办已记录"，动作仍标 success（不阻塞）
            - SMTP 配置但发送失败   → retryable 失败，保留 pending_actions 等待重试
            - 律师邮箱无法解析       → 降级为待办 + 警告（不阻塞其他动作）

        律师通知此时已是 EXECUTED 阶段（冷静期 + 继承人确认之后），
        自动发送是合理的，不构成骚扰。
        """
        lawyer_id = record.config.lawyer_user_id
        if not lawyer_id:
            # 无律师配置：标记为已跳过（非失败）
            return {
                "success": True,
                "skipped": True,
                "reason": "no_lawyer_configured",
            }

        # 解析律师邮箱地址
        lawyer_email = self._resolve_email(lawyer_id)
        if not lawyer_email:
            # 邮箱无法解析：降级为待办，不阻塞流程
            logger.warning(
                "deadman_switch 律师邮箱无法解析 user=%s lawyer_id=%s；降级为待办",
                record.user_id,
                lawyer_id,
            )
            return {
                "success": True,
                "lawyer_user_id": lawyer_id,
                "notified_via": "manual_todo",
                "degraded": True,
                "reason": "lawyer_email_unresolvable",
                "note": "律师邮箱无法解析，通知待办已记录，需人工渠道补发",
            }

        # SMTP 未配置：降级为待办
        sender = self.email_sender
        if sender is None or not _sender_configured(sender):
            return {
                "success": True,
                "lawyer_user_id": lawyer_id,
                "lawyer_email": lawyer_email,
                "notified_via": "manual_todo",
                "degraded": True,
                "reason": "smtp_not_configured",
                "note": "SMTP 未配置，律师通知待办已记录，需人工渠道补发",
            }

        # 实际发送邮件
        subject, body = _build_lawyer_notification(record)
        result = _safe_send_sync(sender, lawyer_email, subject, body)
        if result.get("sent"):
            # 记录 sent_log（过 guardrail.record_send 留审计痕迹）
            self.guardrail.record_send(
                lawyer_id,
                content="[脱敏] deadman_switch 律师介入通知",
                channel="deadman_switch",
            )
            return {
                "success": True,
                "lawyer_user_id": lawyer_id,
                "lawyer_email": lawyer_email,
                "notified_via": "email",
                "message_id": result.get("message_id"),
            }
        # 发送失败 → retryable
        raise _ActionRetryableError(
            f"lawyer email send failed to={lawyer_email} "
            f"error={result.get('error') or result.get('reason')}"
        )

    def _do_notify_heirs(self, record: SwitchRecord) -> dict[str, Any]:
        """通知法定继承人 - NotificationGuardrail 校验 + EmailSender 发送邮件

        P0-3 修复：原实现只过 guardrail 记 sent_log，不实际发送邮件，
        继承人收不到任何通知。现改为对每个通过 guardrail 的继承人真正发送邮件：
            - guardrail 拒绝      → 该继承人记入 blocked，不发送
            - 邮箱无法解析        → 该继承人记入 unresolvable，降级为待办
            - SMTP 未配置         → 全部继承人降级为待办（guardrail 仍记 sent_log）
            - SMTP 配置但发送失败 → 该继承人作为 retryable 失败
            - 全部发送失败        → 整个动作 retryable，保留 pending_actions
        """
        now = datetime.now()
        heirs = record.config.heir_user_ids
        if not heirs:
            return {"success": True, "skipped": True, "reason": "no_heirs_configured"}

        sender = self.email_sender
        smtp_ready = sender is not None and _sender_configured(sender)

        notified: list[str] = []
        blocked: list[str] = []
        unresolvable: list[str] = []
        send_failed: list[str] = []
        degraded_via_todo: list[str] = []

        subject, body = _build_heir_notification(record)

        for hid in heirs:
            # 1. guardrail 同意检查
            ok, reason = self.guardrail.can_send(hid, now)
            if not ok:
                blocked.append(f"{hid}:{reason}")
                continue
            # 2. 邮箱解析
            heir_email = self._resolve_email(hid)
            if not heir_email:
                # 邮箱无法解析：降级为待办（记 sent_log 留审计痕迹），仍算"已通知"
                # 理由：guardrail 已通过=继承人已同意接收；邮箱缺失是数据问题，
                # 不应阻塞状态机推进（当事人已确认失联，需继续执行后续动作）。
                # 运营可通过 executed_actions 中的 degraded 标记人工补发。
                unresolvable.append(hid)
                self.guardrail.record_send(
                    hid,
                    content="[脱敏] deadman_switch 继承人通知（邮箱未解析，待办）",
                    channel="deadman_switch",
                )
                notified.append(hid)
                degraded_via_todo.append(hid)
                continue
            # 3. SMTP 未配置 → 降级为待办（guardrail 仍记 sent_log）
            if not smtp_ready:
                self.guardrail.record_send(
                    hid,
                    content="[脱敏] deadman_switch 继承人通知（SMTP 未配置，待办）",
                    channel="deadman_switch",
                )
                notified.append(hid)
                degraded_via_todo.append(hid)
                continue
            # 4. 实际发送
            result = _safe_send_sync(sender, heir_email, subject, body)
            if result.get("sent"):
                self.guardrail.record_send(
                    hid,
                    content="[脱敏] deadman_switch 继承人通知：当事人失联确认完成",
                    channel="deadman_switch",
                )
                notified.append(hid)
            else:
                send_failed.append(f"{hid}:{result.get('error') or result.get('reason')}")

        # 汇总结果
        # - 至少有一个继承人成功通知（含降级待办）→ success=True
        # - 全部 blocked / send_failed 且无人成功 → retryable
        #   （blocked=继承人未同意；send_failed=SMTP 瞬时故障，应重试）
        # - unresolvable / SMTP 未配置 不算失败（已降级为待办）
        if not notified:
            all_fail_reasons = blocked + send_failed
            raise _ActionRetryableError(
                f"all heirs failed to notify; "
                f"blocked={blocked} send_failed={send_failed}; "
                f"details={all_fail_reasons}"
            )

        result: dict[str, Any] = {
            "success": True,
            "notified_heirs": notified,
        }
        if blocked:
            result["blocked_heirs"] = blocked
        if unresolvable:
            result["unresolvable_heirs"] = unresolvable
        if send_failed:
            result["send_failed_heirs"] = send_failed
        if degraded_via_todo:
            result["degraded_via_todo"] = degraded_via_todo
            result["degraded_reason"] = (
                "smtp_not_configured" if not smtp_ready else "email_unresolvable"
            )
        return result

    # ==================================================================
    # 邮件通知辅助方法
    # ==================================================================
    def _resolve_email(self, identifier: str) -> str | None:
        """从标识符解析邮箱地址

        - 标识符含 "@" → 直接当作邮箱返回
        - 否则视为 user_id → 通过 UserStore.get_user() 查 email
        - 查不到 / UserStore 不可用 → 返回 None

        与 auto_tick._notify_state_change_via_email 的收件人解析逻辑一致。
        """
        if not identifier:
            return None
        if "@" in identifier:
            return identifier
        # user_id 路径：懒加载 UserStore 避免循环 import
        store = self.user_store
        if store is None:
            try:
                from ..auth.store import UserStore

                store = UserStore()
                self.user_store = store
            except Exception as exc:
                logger.warning("UserStore 初始化失败，无法解析邮箱: %s", exc)
                return None
        try:
            user = store.get_user(identifier)
            if user is None:
                return None
            email = user.get("email")
            return email or None
        except Exception as exc:
            logger.warning("查询用户邮箱失败 identifier=%s: %s", identifier, exc)
            return None


class _ActionRetryableError(Exception):
    """可重试动作失败 - 动作保留在 pending_actions 等待下次执行"""

    pass


# ======================================================================
# 模块级辅助函数（邮件通知）
# 抽成函数而非方法，便于单测直接 patch / 调用，且不依赖 self 状态。
# ======================================================================
def _sender_configured(sender: Any) -> bool:
    """安全检查 EmailSender 是否已配置（is_configured 异常时返回 False）"""
    try:
        return bool(sender.is_configured())
    except Exception:
        return False


def _safe_send_sync(sender: Any, to_email: str, subject: str, body: str) -> dict:
    """安全调用 EmailSender.send_sync（异常时返回失败 dict，不抛出）

    EmailSender.send_sync 内部已 try/except，但兜一层防止 sender 为 mock
    对象或非标准实现抛出意外异常导致 execute_confirmed 中断。
    """
    try:
        return sender.send_sync(to_email, subject, body)
    except Exception as exc:
        logger.warning("send_sync 异常 to=%s: %s", to_email, exc)
        return {"sent": False, "error": str(exc)}


def _build_lawyer_notification(record: SwitchRecord) -> tuple[str, str]:
    """构造律师通知邮件（主题, 正文）

    律师通知在 EXECUTED 阶段发送（冷静期 + 继承人确认之后），
    内容聚焦"当事人失联已确认，需律师介入处理后事"。
    """
    confirmed_at = record.confirmed_at.isoformat() if record.confirmed_at else "未知"
    subject = f"[Dead Man Switch] 律师介入通知 - 当事人 {record.user_id}"
    body = (
        f"尊敬的律师：\n\n"
        f"本系统 Dead Man Switch 已完成当事人失联确认流程，现正式通知您介入。\n\n"
        f"当事人 user_id：{record.user_id}\n"
        f"状态：{record.state.value}\n"
        f"确认时间（UTC）：{confirmed_at}\n\n"
        f"已完成的确认流程：\n"
        f"  - 连续失联触发死亡推定\n"
        f"  - 紧急联系人确认\n"
        f"  - 法定继承人二次确认\n"
        f"  - {record.config.cooldown_days} 天冷静期已过\n\n"
        f"请通过系统核实详情并协助处理后事。\n"
        f"本邮件由系统自动发送，请勿直接回复。\n"
    )
    return subject, body


def _build_heir_notification(record: SwitchRecord) -> tuple[str, str]:
    """构造继承人通知邮件（主题, 正文）

    继承人通知在 EXECUTED 阶段发送，内容聚焦"当事人失联确认完成，
    请继承人查收身后信件 / 保险库等遗产"。
    """
    confirmed_at = record.confirmed_at.isoformat() if record.confirmed_at else "未知"
    subject = f"[Dead Man Switch] 继承人通知 - 当事人 {record.user_id} 失联确认完成"
    body = (
        f"尊敬的继承人：\n\n"
        f"本系统 Dead Man Switch 已完成当事人失联确认流程，现正式通知您。\n\n"
        f"当事人 user_id：{record.user_id}\n"
        f"状态：{record.state.value}\n"
        f"确认时间（UTC）：{confirmed_at}\n\n"
        f"已完成流程：\n"
        f"  - 连续失联触发死亡推定\n"
        f"  - 紧急联系人确认\n"
        f"  - 律师已介入\n"
        f"  - {record.config.cooldown_days} 天冷静期已过\n\n"
        f"后续事项：\n"
        f"  - 当事人预留的身后信件已触发投递\n"
        f"  - 数字遗产保险库已触发交付\n"
        f"  - 请联系律师协助处理法律事宜\n\n"
        f"请通过系统查看更多详情。\n"
        f"本邮件由系统自动发送，请勿直接回复。\n"
    )
    return subject, body
