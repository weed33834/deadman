"""EmailSender - 邮件通知发送器（aiosmtplib 异步 + smtplib 同步双实现）

用于 Dead Man Switch 状态变更时通知律师 / 继承人 / 紧急联系人。

设计原则：
    - 默认降级：DEADMAN_SMTP_HOST 留空时 is_configured()=False，
      send()/send_sync() 直接返回 smtp_not_configured，不报错（仅记录 pending 待办）
    - 韧性优先：aiosmtplib 未安装 / 发送异常均不抛出，降级为返回错误 dict
    - 双轨发送：
        * send()       —— async，基于 aiosmtplib，供 auto_tick 等异步调度器使用
        * send_sync()  —— 同步，基于 stdlib smtplib，供 SwitchActionExecutor
                          这类同步执行器在 to_thread 上下文中调用
      两者共享同一份配置和邮件构造逻辑，行为一致。
    - 配置来源：全部从环境变量读取（见 .env.example 邮件通知段）
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

logger = logging.getLogger(__name__)


class EmailSender:
    """邮件通知发送器 - 异步（aiosmtplib）+ 同步（smtplib）双实现

    用法::

        sender = EmailSender()
        if sender.is_configured():
            # 异步上下文（auto_tick 等）
            result = await sender.send("lawyer@example.com", "主题", "正文")
            # 同步上下文（SwitchActionExecutor 等，常在 to_thread 中运行）
            result = sender.send_sync("lawyer@example.com", "主题", "正文")
    """

    def __init__(self) -> None:
        self.host: str = os.getenv("DEADMAN_SMTP_HOST", "")
        self.port: int = int(os.getenv("DEADMAN_SMTP_PORT", "587"))
        self.user: str = os.getenv("DEADMAN_SMTP_USER", "")
        self.password: str = os.getenv("DEADMAN_SMTP_PASSWORD", "")
        self.from_addr: str = os.getenv("DEADMAN_SMTP_FROM", "noreply@deadman.local")
        # "1"=启用 STARTTLS（默认，适配 587 端口）；其它值=禁用
        self.use_tls: bool = os.getenv("DEADMAN_SMTP_USE_TLS", "1") == "1"

    def is_configured(self) -> bool:
        """检查 SMTP 是否已配置（host 非空即视为已配置）"""
        return bool(self.host)

    def _build_message(self, to_email: str, subject: str, body: str) -> tuple[EmailMessage, str]:
        """构造 EmailMessage（异步/同步路径共用）

        Returns:
            (msg, message_id) —— message_id 已写入 msg["Message-ID"]
        """
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to_email
        msg["Subject"] = subject
        message_id = make_msgid()
        msg["Message-ID"] = message_id
        msg.set_content(body)
        return msg, message_id

    async def send(self, to_email: str, subject: str, body: str) -> dict:
        """异步发送邮件

        Args:
            to_email: 收件人邮箱
            subject: 邮件主题
            body: 邮件正文（纯文本）

        Returns:
            SMTP 未配置：{"sent": False, "reason": "smtp_not_configured"}
            发送成功：  {"sent": True, "message_id": ...}
            发送异常：  {"sent": False, "error": str(exc)}
        """
        if not self.is_configured():
            return {"sent": False, "reason": "smtp_not_configured"}

        # aiosmtplib 懒加载：未安装时降级为错误 dict，而非 ImportError 中断调用方
        try:
            import aiosmtplib  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning("aiosmtplib 未安装，邮件发送降级: %s", exc)
            return {"sent": False, "error": f"aiosmtplib_not_installed: {exc}"}

        msg, message_id = self._build_message(to_email, subject, body)

        try:
            if self.use_tls:
                # STARTTLS：先明文连接再升级（适配 587 端口，最常见）
                await aiosmtplib.send(
                    msg,
                    hostname=self.host,
                    port=self.port,
                    username=self.user or None,
                    password=self.password or None,
                    start_tls=True,
                )
            else:
                await aiosmtplib.send(
                    msg,
                    hostname=self.host,
                    port=self.port,
                    username=self.user or None,
                    password=self.password or None,
                )
            logger.info("邮件发送成功 to=%s subject=%s", to_email, subject)
            return {"sent": True, "message_id": message_id}
        except Exception as exc:
            logger.warning("邮件发送失败 to=%s subject=%s: %s", to_email, subject, exc)
            return {"sent": False, "error": str(exc)}

    def send_sync(self, to_email: str, subject: str, body: str) -> dict:
        """同步发送邮件（基于 stdlib smtplib，无第三方依赖）

        供 SwitchActionExecutor 这类同步执行器调用（常通过 asyncio.to_thread
        在线程池中运行）。返回结构与 :meth:`send` 完全一致，便于上层统一处理。

        Args:
            to_email: 收件人邮箱
            subject: 邮件主题
            body: 邮件正文（纯文本）

        Returns:
            SMTP 未配置：{"sent": False, "reason": "smtp_not_configured"}
            发送成功：  {"sent": True, "message_id": ...}
            发送异常：  {"sent": False, "error": str(exc)}
        """
        if not self.is_configured():
            return {"sent": False, "reason": "smtp_not_configured"}

        msg, message_id = self._build_message(to_email, subject, body)

        try:
            # 使用 with 语法确保连接关闭；SMTP/SMTP_SSL 按 use_tls 选择
            # 注意：smtplib.SMTP 的 start_tls() 是显式方法，与 aiosmtplib 的
            # start_tls 参数不同，这里手动调用以适配 587 端口 STARTTLS 流程。
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.ehlo()
                if self.use_tls:
                    server.starttls()
                    server.ehlo()
                if self.user:
                    server.login(self.user, self.password)
                server.send_message(msg)
            logger.info("邮件发送成功(sync) to=%s subject=%s", to_email, subject)
            return {"sent": True, "message_id": message_id}
        except Exception as exc:
            logger.warning("邮件发送失败(sync) to=%s subject=%s: %s", to_email, subject, exc)
            return {"sent": False, "error": str(exc)}
