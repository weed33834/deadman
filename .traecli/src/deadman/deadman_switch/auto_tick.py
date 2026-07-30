"""Dead Man Switch 自动 tick 调度器

补齐状态机断裂点：SwitchStore.tick(user_id) 已实现状态机推进
（ACTIVE → SUSPECTED → VERIFYING → CONFIRMED → EXECUTED），但缺少
后台定时任务遍历所有用户调用 tick。本模块提供轻量的 asyncio 调度器，
不依赖外部服务（Celery / systemd timer 等）。

调度流程（每 interval 秒一次）：
    1. SwitchStore.list_all_users() 取全部已初始化的 user_id
    2. 对每个 user_id 调用 store.tick(user_id)
    3. 若状态进入 VERIFYING 且 config.lawyer_user_id 已设置但
       lawyer_engaged=False：自动调 engage_lawyer（模拟律师自动介入）
    4. 若状态进入 CONFIRMED 且冷静期已过（is_cooldown_passed）：
       自动调 SwitchActionExecutor.execute_confirmed(user_id)
       执行预设动作（投递身后信件 / 通知律师 / 通知继承人 等）

异常隔离：单用户处理失败不影响其他用户；主循环异常被捕获后继续。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .actions import SwitchActionExecutor
from .models import SwitchRecord, SwitchState
from .store import SwitchStore

logger = logging.getLogger(__name__)


class SwitchAutoTicker:
    """Dead Man Switch 自动 tick 调度器

    用法（独立运行）::

        import asyncio
        from deadman.deadman_switch.auto_tick import SwitchAutoTicker
        from deadman.deadman_switch.store import SwitchStore

        ticker = SwitchAutoTicker(SwitchStore())
        asyncio.run(ticker.run_forever(interval_seconds=300))

    Web Server 中通过 threading.Thread 启动一个独立 asyncio 事件循环
    运行本调度器（见 web/server.py 的 _start_switch_auto_ticker）。
    """

    def __init__(
        self,
        store: SwitchStore,
        executor: SwitchActionExecutor | None = None,
        email_sender: Any = None,
    ) -> None:
        self.store = store
        # 邮件通知器：注入优先；未注入则懒加载 EmailSender
        # （EmailSender 初始化只读环境变量不报错；aiosmtplib 不可用时在 send() 内部降级）
        self.email_sender = email_sender
        if self.email_sender is None:
            try:
                from ..notification.email_sender import EmailSender

                self.email_sender = EmailSender()
            except Exception as exc:
                logger.warning("EmailSender 初始化失败，邮件通知降级: %s", exc)
                self.email_sender = None
        # 注入 executor 便于测试隔离（不传则用默认 NotificationGuardrail 数据目录）
        # 同时把 email_sender 传入 executor，使 EXECUTED 阶段的 notify_lawyer /
        # notify_heirs 动作能复用同一份 SMTP 配置真正发送邮件（P0-3 修复）
        if executor is None:
            self.executor = SwitchActionExecutor(store=store, email_sender=self.email_sender)
        else:
            self.executor = executor
        # 主循环控制位（run_forever 内部置 False 后退出）
        self._running = False

    # ==================================================================
    # 单次执行
    # ==================================================================
    async def tick_once(self) -> dict[str, Any]:
        """单次扫描所有用户并推进状态机

        供测试和手动触发（如 CLI switch-auto-tick）调用。
        单个用户处理异常不影响其他用户。

        Returns:
            汇总信息 dict：
                {
                    "scanned": int,            扫描到的用户总数
                    "ticked": int,             成功调用 tick 的用户数
                    "lawyer_engaged": int,     自动触发 engage_lawyer 次数
                    "executed": int,           自动执行 CONFIRMED 动作次数
                    "emails_attempted": int,   尝试发送通知邮件次数
                    "emails_sent": int,        成功发送通知邮件次数
                    "errors": list[str],       错误信息（user_id + reason）
                }
        """
        result: dict[str, Any] = {
            "scanned": 0,
            "ticked": 0,
            "lawyer_engaged": 0,
            "executed": 0,
            "emails_attempted": 0,
            "emails_sent": 0,
            "errors": [],
        }
        try:
            user_ids = self.store.list_all_users()
        except Exception as exc:
            # list_all_users 内部已 try/except，但兜一层防止主循环挂掉
            logger.exception("list_all_users 异常: %s", exc)
            result["errors"].append(f"list_all_users: {exc}")
            return result

        result["scanned"] = len(user_ids)
        for user_id in user_ids:
            try:
                advanced = await self._process_user(user_id)
                if advanced.get("ticked"):
                    result["ticked"] += 1
                if advanced.get("lawyer_engaged"):
                    result["lawyer_engaged"] += 1
                if advanced.get("executed"):
                    result["executed"] += 1
                result["emails_attempted"] += int(advanced.get("emails_attempted") or 0)
                result["emails_sent"] += int(advanced.get("emails_sent") or 0)
            except Exception as exc:
                # 单用户失败不影响其他用户
                logger.exception("auto_tick 处理用户失败 user=%s: %s", user_id, exc)
                result["errors"].append(f"{user_id}: {exc}")
        if result["errors"]:
            logger.warning(
                "auto_tick 本轮完成 scanned=%d ticked=%d errors=%d",
                result["scanned"],
                result["ticked"],
                len(result["errors"]),
            )
        else:
            logger.info(
                "auto_tick 本轮完成 scanned=%d ticked=%d lawyer_engaged=%d "
                "executed=%d emails_sent=%d",
                result["scanned"],
                result["ticked"],
                result["lawyer_engaged"],
                result["executed"],
                result["emails_sent"],
            )
        return result

    async def _process_user(self, user_id: str) -> dict[str, Any]:
        """处理单个用户：tick → 必要时 engage_lawyer → 必要时 execute_confirmed

        所有步骤异常由上层 tick_once 兜底；本方法内部仅抛出真实异常。
        """
        out: dict[str, Any] = {
            "ticked": False,
            "lawyer_engaged": False,
            "executed": False,
            "emails_attempted": 0,
            "emails_sent": 0,
        }
        # 记录 tick 前状态，用于检测是否"进入" SUSPECTED/VERIFYING
        # （仅在状态转换瞬间通知，避免每个 tick 周期重复发送邮件）
        try:
            prev_record = await asyncio.to_thread(self.store.load, user_id)
            prev_state = prev_record.state if prev_record is not None else None
        except Exception:
            prev_state = None
        # tick 主体（同步 IO，放到线程池避免阻塞事件循环）
        record = await asyncio.to_thread(self.store.tick, user_id)
        if record is None:
            # 用户在 list_all_users 与 tick 之间被删除 / 解密失败
            return out
        out["ticked"] = True

        # 状态进入 SUSPECTED/VERIFYING 时：尝试发送通知邮件给紧急联系人和继承人
        # （try/except 保护，EmailSender 不可用 / SMTP 未配置时降级为 no-op）
        if (
            record.state in (SwitchState.SUSPECTED, SwitchState.VERIFYING)
            and prev_state != record.state
        ):
            try:
                attempted, sent = await self._notify_state_change_via_email(record)
                out["emails_attempted"] = attempted
                out["emails_sent"] = sent
                if sent:
                    logger.info(
                        "auto_tick 邮件通知已发送 user=%s state=%s sent=%d",
                        user_id,
                        record.state.value,
                        sent,
                    )
            except Exception as exc:
                logger.warning("auto_tick 邮件通知流程异常 user=%s: %s", user_id, exc)

        # VERIFYING 状态：律师自动介入
        # （_check_verification_complete 要求 lawyer_engaged=True 才能推进到 CONFIRMED）
        if (
            record.state == SwitchState.VERIFYING
            and record.config.lawyer_user_id
            and not record.lawyer_engaged
        ):
            try:
                rec, msg = await asyncio.to_thread(self.store.engage_lawyer, user_id)
                if rec is not None and msg == "lawyer_engaged":
                    out["lawyer_engaged"] = True
                    logger.info(
                        "auto_tick 自动律师介入 user=%s lawyer=%s",
                        user_id,
                        rec.config.lawyer_user_id,
                    )
                # engage_lawyer 可能改状态机（VERIFYING→CONFIRMED 在下一次 tick 推进）
                # 重新调一次 tick 让 _check_verification_complete 复评
                if rec is not None:
                    record = await asyncio.to_thread(self.store.tick, user_id)
                    if record is None:
                        return out
            except Exception as exc:
                logger.warning("auto_tick engage_lawyer 失败 user=%s: %s", user_id, exc)

        # CONFIRMED 状态且冷静期已过 → 自动执行预设动作
        if record.state == SwitchState.CONFIRMED:
            cooldown_passed = await asyncio.to_thread(self.store.is_cooldown_passed, user_id)
            if cooldown_passed:
                try:
                    exec_result = await asyncio.to_thread(self.executor.execute_confirmed, user_id)
                    out["executed"] = True
                    logger.info(
                        "auto_tick 自动执行完成 user=%s executed=%d failed=%d state=%s",
                        user_id,
                        len(exec_result.get("executed", [])),
                        len(exec_result.get("failed", [])),
                        exec_result.get("state"),
                    )
                except RuntimeError as exc:
                    # 冷静期 / 状态检查不通过：属于正常业务逻辑，仅 debug 日志
                    logger.debug(
                        "auto_tick execute_confirmed 被拒绝 user=%s: %s",
                        user_id,
                        exc,
                    )
                except Exception as exc:
                    logger.warning(
                        "auto_tick execute_confirmed 失败 user=%s: %s",
                        user_id,
                        exc,
                    )
        return out

    # ==================================================================
    # 邮件通知（状态进入 SUSPECTED/VERIFYING 时触发）
    # ==================================================================
    async def _notify_state_change_via_email(self, record: SwitchRecord) -> tuple[int, int]:
        """状态进入 SUSPECTED/VERIFYING 时尝试发送通知邮件给紧急联系人和继承人

        所有异常被捕获，绝不影响主 tick 流程。返回 (attempted, sent) 计数。

        收件人解析：emergency_contacts / heir_user_ids 中存的是 user_id
        （PIPL 合规要求不存储原始 PII），仅当其形如邮箱（含 "@"）时才发送，
        其余跳过——既避免向 user_id 发垃圾，也保证未配置邮箱时静默降级。
        """
        attempted = 0
        sent = 0
        sender = self.email_sender
        if sender is None:
            return attempted, sent
        try:
            if not sender.is_configured():
                return attempted, sent
        except Exception as exc:
            logger.warning("EmailSender.is_configured 异常: %s", exc)
            return attempted, sent

        # 收件人：紧急联系人 + 法定继承人，去重，仅取形如邮箱的标识符
        recipients: list[str] = []
        for rid in list(record.config.emergency_contacts) + list(record.config.heir_user_ids):
            if "@" in rid and rid not in recipients:
                recipients.append(rid)
        if not recipients:
            return attempted, sent

        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        subject = f"[Dead Man Switch] 当事人状态变更：{record.state.value}"
        body = (
            f"Dead Man Switch 状态机已进入 {record.state.value} 阶段。\n"
            f"当事人 user_id：{record.user_id}\n"
            f"时间（UTC）：{now_iso}\n\n"
            f"请通过系统核实当事人安好状况。\n"
            f"本邮件由系统自动发送，请勿直接回复。"
        )
        for to_email in recipients:
            attempted += 1
            try:
                result = await sender.send(to_email, subject, body)
                if result.get("sent"):
                    sent += 1
                else:
                    logger.info(
                        "auto_tick 邮件通知未发送 user=%s to=%s reason=%s",
                        record.user_id,
                        to_email,
                        result.get("reason") or result.get("error"),
                    )
            except Exception as exc:
                logger.warning(
                    "auto_tick 邮件通知异常 user=%s to=%s: %s",
                    record.user_id,
                    to_email,
                    exc,
                )
        return attempted, sent

    # ==================================================================
    # 主循环
    # ==================================================================
    async def run_forever(self, interval_seconds: int = 300) -> None:
        """主循环 - 每 interval_seconds 秒扫描一次所有用户

        所有异常捕获并记日志，不让主循环崩溃。
        通过设置 self._running=False 优雅退出（也可被 cancel）。
        """
        self._running = True
        logger.info(
            "SwitchAutoTicker 主循环启动 interval=%ss data_dir=%s",
            interval_seconds,
            self.store.data_dir,
        )
        while self._running:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                logger.info("SwitchAutoTicker 收到取消信号，正在停止...")
                raise
            except Exception as exc:
                # tick_once 内部已兜底；此处再兜一层防止主循环挂掉
                logger.exception("SwitchAutoTicker tick_once 异常（主循环继续）: %s", exc)
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                logger.info("SwitchAutoTicker 收到取消信号，正在停止...")
                raise

    def stop(self) -> None:
        """请求主循环退出（设置 _running=False，下一轮 sleep 后生效）"""
        self._running = False
