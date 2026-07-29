"""EmailSender - 邮件通知发送器（aiosmtplib 异步实现）

用于 Dead Man Switch 状态变更时通知律师 / 继承人 / 紧急联系人。

设计原则：
    - 默认降级：DEADMAN_SMTP_HOST 留空时 is_configured()=False，
      send() 直接返回 smtp_not_configured，不报错（仅记录 pending 待办）
    - 韧性优先：aiosmtplib 未安装 / 发送异常均不抛出，降级为返回错误 dict
    - 异步发送：send() 为 async，配合 auto_tick 等异步调度器
    - 配置来源：全部从环境变量读取（见 .env.example 邮件通知段）
"""

from __future__ import annotations

import logging
import os
from email.message import EmailMessage
from email.utils import make_msgid

logger = logging.getLogger(__name__)


class EmailSender:
    """邮件通知发送器 - 基于 aiosmtplib 的异步实现

    用法::

        sender = EmailSender()
        if sender.is_configured():
            result = await sender.send("lawyer@example.com", "主题", "正文")
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

        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to_email
        msg["Subject"] = subject
        message_id = make_msgid()
        msg["Message-ID"] = message_id
        msg.set_content(body)

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
