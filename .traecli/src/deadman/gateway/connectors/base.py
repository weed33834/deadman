"""PlatformConnector 抽象 - 平台连接器协议

所有平台连接器（Telegram/微信/邮件/Webhook）必须满足此 Protocol。
借鉴 Hermes Agent (MIT License) 的 platform registry 模式，但简化为单文件抽象。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from collections.abc import AsyncIterator


@runtime_checkable
class PlatformConnector(Protocol):
    """平台连接器抽象 - 所有消息平台连接器必须满足此协议。

    实现方需提供：
        - platform_name: 平台标识（telegram/wechat/email/webhook）
        - start(): 启动连接（建链、登录、注册 webhook 等）
        - stop(): 停止连接，释放资源
        - send(user_id, text): 发送消息给指定用户
        - poll(): 异步迭代入站消息，yield (user_id, text)
    """

    platform_name: str

    async def start(self) -> None:
        """启动连接器（建链、登录等）"""
        ...

    async def stop(self) -> None:
        """停止连接器，释放资源"""
        ...

    async def send(self, user_id: str, text: str) -> bool:
        """发送消息给指定用户。

        Args:
            user_id: deadman 用户 ID（或平台 chat_id，由 connector 内部解析）
            text: 消息文本

        Returns:
            True 表示发送成功，False 表示失败
        """
        ...

    def poll(self) -> AsyncIterator[tuple[str, str]]:
        """异步迭代入站消息。

        Yields:
            (user_id, text) 元组，user_id 为 deadman 用户 ID
        """
        ...
