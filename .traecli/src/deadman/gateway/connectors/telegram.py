"""Telegram Bot 连接器 - 借鉴 Hermes 但简化

借鉴 Hermes Agent (MIT License) 的 `plugins/platforms/telegram/adapter.py` 设计，
但适配 deadman 轻量部署场景：

与 Hermes 的差异：
    - 不用 PTB 库（避免重依赖），用 httpx 直连 Bot API
    - 不支持 inline keyboard / sticker / voice（deadman 场景不需要）
    - 入站消息长轮询 getUpdates
    - 配对：用户 /start <token> 绑定 deadman user_id
    - 无 bot token 时 start() 打印警告并优雅降级（不抛异常）

约束：
    - 不依赖网络（测试时 mock httpx 调用）
    - 入站消息不受 NotificationGuardrail 约束（用户主动询问）
    - 但 /stop 退订命令直接调 guard.record_unsubscribe()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


# 长轮询超时（秒），Telegram getUpdates 的 timeout 参数
_POLL_TIMEOUT_SECONDS = 30


class TelegramConnector:
    """Telegram Bot 连接器 - httpx 直连 Bot API。

    用法：
        conn = TelegramConnector(bot_token="...", pairing_tokens={"abc123": "user-1"})
        await conn.start()
        async for user_id, text in conn.poll():
            ...
        await conn.send("user-1", "你好")
        await conn.stop()
    """

    platform_name = "telegram"

    def __init__(
        self,
        bot_token: str,
        pairing_tokens: dict[str, str] | None = None,
        guard: Any | None = None,
    ) -> None:
        """初始化 Telegram 连接器。

        Args:
            bot_token: Telegram Bot API token（无则 start() 优雅降级）
            pairing_tokens: 配对 token 表 {token: deadman_user_id}
            guard: NotificationGuardrail 实例（用于 /stop 退订）
        """
        self.bot_token = bot_token or ""
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
        self.pairing_tokens: dict[str, str] = pairing_tokens or {}
        self._guard = guard
        self._offset: int = 0
        self._running: bool = False
        # telegram_chat_id -> deadman_user_id（已配对用户）
        self._paired: dict[int, str] = {}
        # deadman_user_id -> telegram_chat_id（反查）
        self._user_to_chat: dict[str, int] = {}

    # ==================================================================
    # start / stop
    # ==================================================================

    async def start(self) -> None:
        """启动连接器。

        无 bot_token 时打印警告并优雅降级（不抛异常），
        poll() 在此情况下不会 yield 任何消息。
        """
        if not self.bot_token:
            logger.warning(
                "TelegramConnector 未配置 bot_token，"
                "start() 优雅降级，poll() 不会拉取消息。"
                "请通过 DEADMAN_TELEGRAM_BOT_TOKEN 环境变量配置。"
            )
            return

        # 通过 getMe 校验 token
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.api_base}/getMe")
                if resp.status_code != 200:
                    logger.warning("Telegram getMe 失败 status=%s", resp.status_code)
                    return
                data = resp.json()
                if not data.get("ok"):
                    logger.warning("Telegram getMe 返回 ok=false: %s", data)
                    return
                bot_username = data.get("result", {}).get("username", "?")
                logger.info("Telegram bot 已连接: @%s", bot_username)
        except Exception as exc:
            logger.warning("Telegram getMe 异常（降级为不拉取）: %s", exc)
            return

        self._running = True

    async def stop(self) -> None:
        """停止连接器"""
        self._running = False

    # ==================================================================
    # send - 主动发送消息
    # ==================================================================

    async def send(self, chat_id_or_user_id: str, text: str) -> bool:
        """发送消息给指定 chat 或已配对用户。

        Args:
            chat_id_or_user_id: Telegram chat_id（数字字符串）或 deadman user_id
            text: 消息文本

        Returns:
            True 表示发送成功，False 表示失败
        """
        if not self.bot_token or not self.api_base:
            logger.warning("TelegramConnector.send 失败：未配置 bot_token")
            return False

        chat_id = self._resolve_chat_id(chat_id_or_user_id)
        if chat_id is None:
            logger.warning("无法解析 chat_id: %s（用户未配对？）", chat_id_or_user_id)
            return False

        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.api_base}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Telegram sendMessage 失败 status=%s body=%s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return False
                data = resp.json()
                return bool(data.get("ok"))
        except Exception as exc:
            logger.exception("Telegram send 异常: %s", exc)
            return False

    def _resolve_chat_id(self, chat_id_or_user_id: str) -> int | None:
        """解析 chat_id：优先用数字，否则查 _user_to_chat 反查"""
        # 纯数字直接当 chat_id
        try:
            return int(chat_id_or_user_id)
        except (ValueError, TypeError):
            pass
        # 查 deadman user_id → chat_id
        return self._user_to_chat.get(chat_id_or_user_id)

    # ==================================================================
    # poll - 长轮询入站消息
    # ==================================================================

    async def poll(self) -> AsyncIterator[tuple[str, str]]:
        """长轮询 getUpdates，yield (deadman_user_id, text)。

        处理特殊命令：
            - /start <token>: 配对，绑定 telegram_chat_id ↔ deadman_user_id
            - /stop: 退订（调 guard.record_unsubscribe，立即生效）
            - /help: 返回帮助文本

        无 bot_token 时直接返回（不 yield 任何消息）。
        """
        if not self.bot_token or not self.api_base:
            logger.info("TelegramConnector.poll: 无 bot_token，跳过")
            return

        try:
            import httpx
        except ImportError:
            logger.error("httpx 不可用，TelegramConnector 无法 poll")
            return

        async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_SECONDS + 5.0) as client:
            while self._running:
                try:
                    resp = await client.get(
                        f"{self.api_base}/getUpdates",
                        params={
                            "offset": self._offset,
                            "timeout": _POLL_TIMEOUT_SECONDS,
                            "allowed_updates": "message",
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("getUpdates 异常: %s，1 秒后重试", exc)
                    await asyncio.sleep(1.0)
                    continue

                if resp.status_code != 200:
                    logger.warning("getUpdates HTTP %s，1 秒后重试", resp.status_code)
                    await asyncio.sleep(1.0)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    logger.warning("getUpdates ok=false: %s", data)
                    await asyncio.sleep(1.0)
                    continue

                for update in data.get("result", []):
                    self._offset = update.get("update_id", self._offset) + 1
                    message = update.get("message")
                    if not message:
                        continue
                    chat_id = message.get("chat", {}).get("id")
                    text = message.get("text", "").strip()
                    if chat_id is None or not text:
                        continue

                    # 处理特殊命令
                    handled = await self._handle_command(chat_id, text)
                    if handled:
                        continue

                    # 仅 yield 已配对用户的消息
                    user_id = self._paired.get(chat_id)
                    if user_id is None:
                        # 未配对用户主动发消息 → 提示 /start <token>
                        await self._send_text(chat_id, "请先用 /start <token> 完成配对。")
                        continue

                    yield user_id, text

    async def _handle_command(self, chat_id: int, text: str) -> bool:
        """处理特殊命令。返回 True 表示已处理，不应再 yield 给上游。"""
        # /start <token> 配对
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self._send_text(chat_id, "请提供配对 token：/start <token>")
                return True
            token = parts[1].strip()
            user_id = self.pairing_tokens.get(token)
            if user_id is None:
                await self._send_text(chat_id, "配对 token 无效，请检查后重试。")
                return True
            # 配对成功
            self._paired[chat_id] = user_id
            self._user_to_chat[user_id] = chat_id
            await self._send_text(chat_id, f"配对成功，deadman 用户 ID：{user_id}")
            logger.info("Telegram 用户配对成功 chat_id=%s user_id=%s", chat_id, user_id)
            return True

        # /stop 退订
        if text.strip() in ("/stop", "STOP", "0"):
            user_id = self._paired.get(chat_id)
            if user_id and self._guard is not None:
                self._guard.record_unsubscribe(user_id, scope="all")
                await self._send_text(chat_id, "已退订所有主动通知。重新订阅请发 /start <token>。")
                logger.info("Telegram 用户退订 user_id=%s", user_id)
            else:
                await self._send_text(chat_id, "退订请求已收到（当前未配对或无 guard）。")
            return True

        # /help
        if text.strip() == "/help":
            await self._send_text(
                chat_id,
                "deadman 身后事引导平台\n"
                "/start <token> - 配对账户\n"
                "/stop - 退订所有主动通知\n"
                "/help - 显示此帮助\n",
            )
            return True

        return False

    async def _send_text(self, chat_id: int, text: str) -> None:
        """直接给 chat_id 发文本（不记入推送统计）"""
        if not self.bot_token or not self.api_base:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except Exception as exc:
            logger.warning("Telegram _send_text 失败: %s", exc)
