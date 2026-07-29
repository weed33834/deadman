"""测试 deadman.gateway.core - 消息平台 Gateway

覆盖 5 个测试场景：
    - test_handle_inbound_calls_graph: mock graph.ainvoke，验证 handle_inbound 调用它
    - test_send_proactive_blocked_by_guardrail: mock guardrail.can_send 返回 False，验证不发送
    - test_send_proactive_sanitizes_content: 验证内容脱敏
    - test_send_proactive_appends_unsubscribe_hint: 验证退订入口
    - test_send_proactive_records_send: 验证发送后调 record_send

测试隔离：guardrail 用 tmp_path，graph/connector 用 mock，不触达真实网络。
不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deadman.gateway.core import Gateway
from deadman.notification.guardrail import NotificationGuardrail

# =====================================================================
# 辅助：构造带 mock graph + mock connector 的 Gateway
# =====================================================================


def _make_gateway(
    tmp_path: Path,
    graph: MagicMock | None = None,
    connector: MagicMock | None = None,
    guard: NotificationGuardrail | None = None,
) -> Gateway:
    """构造一个用 mock 依赖的 Gateway"""
    if guard is None:
        guard = NotificationGuardrail(data_dir=tmp_path)
    if graph is None:
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "final_response": "测试响应",
                "current_agent": "death-aftercare",
            }
        )
    gw = Gateway(guard=guard, memory_manager=None, graph=graph)
    if connector is not None:
        gw.connectors["mock"] = connector
    return gw


# =====================================================================
# 1. handle_inbound 调用 graph.ainvoke
# =====================================================================


class TestHandleInboundCallsGraph:
    """handle_inbound 应调 graph.ainvoke 获取响应"""

    def test_handle_inbound_calls_graph(self, tmp_path: Path):
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "final_response": "户籍注销需要身份证",
                "current_agent": "death-aftercare",
            }
        )
        gw = _make_gateway(tmp_path, graph=graph)

        # 用 asyncio.run 调 async 方法
        response = asyncio.run(gw.handle_inbound("mock", "u1", "户籍注销要哪些材料"))

        graph.ainvoke.assert_called_once()
        assert response == "户籍注销需要身份证"

        # 校验传入的 state 包含用户输入
        call_args = graph.ainvoke.call_args
        state = call_args.args[0] if call_args.args else call_args.kwargs.get("state")
        assert state is not None
        assert state["user_input"] == "户籍注销要哪些材料"


# =====================================================================
# 2. send_proactive 被 guardrail 拦截时不发送
# =====================================================================


class TestSendProactiveBlockedByGuardrail:
    """guardrail.can_send 返回 False 时，send_proactive 不应调 connector.send"""

    def test_send_proactive_blocked_by_guardrail(self, tmp_path: Path):
        # 构造一个总是返回 False 的 guard
        guard = MagicMock(spec=NotificationGuardrail)
        guard.can_send = MagicMock(return_value=(False, "silent_hours"))
        guard.sanitize_content = MagicMock(return_value="脱敏后内容")
        guard.record_send = MagicMock()

        connector = MagicMock()
        connector.send = AsyncMock(return_value=True)

        gw = _make_gateway(tmp_path, connector=connector, guard=guard)

        ok, reason = asyncio.run(gw.send_proactive("u1", "测试内容", "mock"))

        assert ok is False
        assert reason == "silent_hours"
        # connector.send 不应被调用
        connector.send.assert_not_called()
        # record_send 也不应被调用
        guard.record_send.assert_not_called()


# =====================================================================
# 3. send_proactive 脱敏内容
# =====================================================================


class TestSendProactiveSanitizesContent:
    """send_proactive 应调 sanitize_content，发送脱敏后内容"""

    def test_send_proactive_sanitizes_content(self, tmp_path: Path):
        # 用真实 guard，但 mock can_send 总是返回 True
        guard = NotificationGuardrail(data_dir=tmp_path)
        guard.can_send = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]

        connector = MagicMock()
        connector.send = AsyncMock(return_value=True)

        gw = _make_gateway(tmp_path, connector=connector, guard=guard)

        # 含禁用词 "死亡"（不含更长词 "死亡证明"），应被替换为 "待办事项"
        ok, reason = asyncio.run(gw.send_proactive("u1", "提醒：今天该处理死亡这件事了", "mock"))

        assert ok is True
        # connector.send 应被调用一次
        connector.send.assert_called_once()
        # 校验发送的内容包含脱敏后的词，不含原禁用词
        sent_text = connector.send.call_args.args[1]
        assert "死亡" not in sent_text
        assert "待办事项" in sent_text


# =====================================================================
# 4. send_proactive 附退订入口
# =====================================================================


class TestSendProactiveAppendsUnsubscribeHint:
    """send_proactive 应在内容末尾附退订入口"""

    def test_send_proactive_appends_unsubscribe_hint(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        guard.can_send = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]

        # 测试各渠道退订入口
        for channel, expected_hint in [
            ("telegram", "（回复 STOP 退订）"),
            ("email", "（点击此处退订：unsubscribe）"),
            ("webhook", "（回复 0 退订）"),
            ("wechat", "（回复 0 退订）"),
        ]:
            connector = MagicMock()
            connector.send = AsyncMock(return_value=True)
            gw = Gateway(guard=guard, memory_manager=None, graph=None)
            gw.connectors[channel] = connector

            ok, _ = asyncio.run(gw.send_proactive("u1", "测试内容", channel))
            assert ok is True
            sent_text = connector.send.call_args.args[1]
            assert expected_hint in sent_text, f"渠道 {channel} 应包含退订入口 {expected_hint!r}"


# =====================================================================
# 5. send_proactive 成功后调 record_send
# =====================================================================


class TestSendProactiveRecordsSend:
    """send_proactive 发送成功后应调 record_send"""

    def test_send_proactive_records_send(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        guard.can_send = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]
        guard.record_send = MagicMock()  # type: ignore[method-assign]

        connector = MagicMock()
        connector.send = AsyncMock(return_value=True)

        gw = _make_gateway(tmp_path, connector=connector, guard=guard)

        ok, _ = asyncio.run(gw.send_proactive("u1", "测试内容", "mock"))

        assert ok is True
        # connector.send 应被调用一次
        connector.send.assert_called_once()
        # record_send 应被调用一次
        guard.record_send.assert_called_once()
        # 校验 record_send 的参数
        call_args = guard.record_send.call_args
        assert call_args.args[0] == "u1"  # user_id
        assert call_args.args[2] == "mock"  # channel
        # 内容应包含退订入口（已被 sanitize + append_unsubscribe_hint 处理）
        sent_content = call_args.args[1]
        assert "退订" in sent_content


# =====================================================================
# 6. send_proactive 命中完全不推送关键词时返回失败
# =====================================================================


class TestSendProactiveBlockKeyword:
    """含 '忌日' 等完全不推送关键词时，send_proactive 应返回失败"""

    def test_send_proactive_block_keyword(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        guard.can_send = MagicMock(return_value=(True, ""))  # type: ignore[method-assign]

        connector = MagicMock()
        connector.send = AsyncMock(return_value=True)

        gw = _make_gateway(tmp_path, connector=connector, guard=guard)

        ok, reason = asyncio.run(gw.send_proactive("u1", "今天是逝者的忌日", "mock"))

        assert ok is False
        assert reason == "content_contains_forbidden_keyword"
        # connector.send 不应被调用
        connector.send.assert_not_called()
