"""Gateway 核心 - 消息平台统一接入层

借鉴 Hermes Agent (MIT License) 的 `gateway/run.py` 设计，但适配 deadman 身后事场景。

与 Hermes 的核心差异：
    - 入站消息直接响应（用户主动询问 = opt-in 当前会话，无需 guardrail）
    - 出站主动消息必须过 NotificationGuardrail（notification-guardrails.md L4 硬边界）
    - 不实现 Hermes 的 scale_to_zero / readiness / restart_loop_guard（deadman 是轻量部署）
    - 不实现 pairing 复杂流程，简化为 token 配对

设计原则：
    - 所有主动推送代码路径必须先调 NotificationGuardrail.can_send()
    - 推送内容必须 sanitize_content 脱敏
    - 推送必须附退订入口
    - 入站响应虽不受 guardrail 约束，但仍受 L0-L8 全部规则约束
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

from ..notification.guardrail import NotificationGuardrail
from .connectors.base import PlatformConnector

logger = logging.getLogger(__name__)


# 各渠道退订入口文案（notification-guardrails.md 第二章约束 6）
_UNSUBSCRIBE_HINTS: dict[str, str] = {
    "telegram": "\n\n（回复 STOP 退订）",
    "email": "\n\n（点击此处退订：unsubscribe）",
    "webhook": "\n\n（回复 0 退订）",
    "wechat": "\n\n（回复 0 退订）",
}


class Gateway:
    """消息平台 Gateway - 借鉴 Hermes gateway/run.py 设计，但适配 deadman。

    用法：
        gw = Gateway()
        gw.register_connector("telegram", TelegramConnector(...))
        await gw.start()
        # 入站：connector.poll() → handle_inbound() → 返回响应
        # 出站：send_proactive() → guardrail 检查 → connector.send()
        await gw.stop()
    """

    def __init__(
        self,
        guard: NotificationGuardrail | None = None,
        memory_manager: Any = None,
        graph: Any = None,
    ) -> None:
        """初始化 Gateway。

        Args:
            guard: NotificationGuardrail 实例（默认创建新的）
            memory_manager: MemoryManager 实例（用于 after_turn 更新记忆）
            graph: LangGraph 编排器（用于 handle_inbound 获取响应）
        """
        self.connectors: dict[str, PlatformConnector] = {}
        self.guard: NotificationGuardrail = guard or NotificationGuardrail()
        self.memory_manager = memory_manager
        self.graph = graph
        self._running: bool = False
        self._tasks: list[asyncio.Task] = []

    # ==================================================================
    # Connector 注册与生命周期
    # ==================================================================

    def register_connector(self, name: str, connector: PlatformConnector) -> None:
        """注册平台连接器"""
        self.connectors[name] = connector
        logger.info("Gateway 已注册 connector: %s", name)

    async def start(self) -> None:
        """启动所有已注册 connector 的轮询/长连接。

        为每个 connector 起一个独立 task 持续 poll() 入站消息，
        并把消息分发给 handle_inbound。
        """
        if self._running:
            logger.warning("Gateway 已在运行，忽略重复 start")
            return
        self._running = True
        for name, connector in self.connectors.items():
            await connector.start()
            task = asyncio.create_task(self._poll_loop(name, connector))
            self._tasks.append(task)
            logger.info("Gateway connector 已启动: %s", name)

    async def _poll_loop(self, name: str, connector: PlatformConnector) -> None:
        """轮询某个 connector 的入站消息，分发给 handle_inbound。"""
        try:
            async for user_id, text in connector.poll():
                if not self._running:
                    break
                try:
                    response = await self.handle_inbound(name, user_id, text)
                    if response:
                        await connector.send(user_id, response)
                except Exception as exc:
                    logger.exception("处理入站消息失败 platform=%s user=%s: %s", name, user_id, exc)
        except asyncio.CancelledError:
            logger.info("Gateway poll loop 被取消: %s", name)
            raise
        except Exception as exc:
            logger.exception("Gateway poll loop 异常退出: %s: %s", name, exc)

    async def stop(self) -> None:
        """停止所有 connector 与 poll loop"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for connector in self.connectors.values():
            try:
                await connector.stop()
            except Exception as exc:
                logger.warning("停止 connector 失败: %s", exc)

    # ==================================================================
    # 入站消息处理（用户主动询问 = opt-in 当前会话，无需 guardrail）
    # ==================================================================

    async def handle_inbound(self, platform: str, user_id: str, text: str) -> str:
        """处理入站消息（用户主动发来）。

        - 用户主动询问 = opt-in 当前会话，无需 guardrail
        - 调 orchestration.graph 获取响应
        - 调 MemoryManager.after_turn 更新记忆
        - 返回响应文本

        Args:
            platform: 平台名（telegram/wechat/email/...）
            user_id: deadman 用户 ID
            text: 用户消息文本

        Returns:
            助手响应文本
        """
        logger.info("入站消息 platform=%s user=%s text_len=%d", platform, user_id, len(text))

        # 延迟导入 graph 与 state，避免循环依赖
        if self.graph is None:
            return "[deadman] 编排器未配置，无法处理入站消息"

        try:
            from ..orchestration.state import create_initial_state
        except ImportError as exc:
            logger.error("导入 orchestration.state 失败: %s", exc)
            return "[deadman] 内部错误：无法构建会话状态"

        state = create_initial_state(user_input=text)
        try:
            result = await self.graph.ainvoke(state)
        except Exception as exc:
            logger.exception("graph.ainvoke 失败: %s", exc)
            return "[deadman] 处理请求时出错，请稍后重试"

        response = result.get("final_response", "") if isinstance(result, dict) else ""

        # 更新记忆（若 memory_manager 可用）
        if self.memory_manager is not None and response:
            try:
                await self.memory_manager.after_turn(
                    user_id=user_id,
                    user_input=text,
                    assistant_response=response,
                    agent=result.get("current_agent", "death-aftercare")
                    if isinstance(result, dict)
                    else "death-aftercare",
                )
            except Exception as exc:
                logger.warning("after_turn 更新记忆失败: %s", exc)

        return response

    # ==================================================================
    # 出站主动消息（必须过 guardrail）
    # ==================================================================

    async def send_proactive(
        self, user_id: str, content: str, channel: str = "telegram"
    ) -> tuple[bool, str]:
        """主动出站消息 - 必须过 guardrail。

        严格遵循 notification-guardrails.md 第七章第 2 节的推送前置检查流程：
            1. can_send() 检查
            2. sanitize_content() 脱敏
            3. 附退订入口
            4. 调 connector.send()
            5. 成功后 record_send()

        Args:
            user_id: 用户 ID
            content: 推送内容（脱敏前）
            channel: 渠道（telegram/email/webhook/wechat）

        Returns:
            (是否发送成功, 失败原因)
            失败原因用于日志，不告知用户。
        """
        # 1. 推送前置检查
        allowed, reason = self.guard.can_send(user_id, datetime.now())
        if not allowed:
            logger.info("推送被拦截 user=%s reason=%s", user_id, reason)
            return False, reason

        # 2. 内容脱敏
        sanitized = self.guard.sanitize_content(content)
        if not sanitized:
            # 命中"完全不推送"关键词
            return False, "content_contains_forbidden_keyword"

        # 3. 附退订入口
        sanitized = self._append_unsubscribe_hint(sanitized, channel)

        # 4. 发送
        connector = self.connectors.get(channel)
        if connector is None:
            return False, f"connector_not_found:{channel}"
        try:
            ok = await connector.send(user_id, sanitized)
        except Exception as exc:
            logger.exception("connector.send 异常 channel=%s: %s", channel, exc)
            return False, "send_failed"

        # 5. 记录已发送
        if ok:
            self.guard.record_send(user_id, sanitized, channel)
        return ok, "" if ok else "send_failed"

    def _append_unsubscribe_hint(self, content: str, channel: str) -> str:
        """每条推送附退订入口（notification-guardrails.md 第二章约束 6）"""
        hint = _UNSUBSCRIBE_HINTS.get(channel, _UNSUBSCRIBE_HINTS["webhook"])
        return content + hint
