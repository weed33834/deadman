"""P4.1 Handoff 一等公民 - 测试矩阵

覆盖点：
1. test_handoff_create_with_llm_compression: LLM 压缩消息历史
2. test_handoff_llm_unavailable_falls_back_to_truncation: LLM 不可用降级到 [:500] 截断
3. test_handoff_filter_rules_whitelist: 白名单过滤 context_vars
4. test_handoff_filter_rules_blacklist: 黑名单过滤
5. test_handoff_apply_to_target: 目标 agent 应用 handoff
6. test_handoff_disabled_no_change: feature flag 关闭无影响
7. test_handoff_context_variables_passed: context vars 跨 agent 传递
"""

from __future__ import annotations

from typing import Any

import pytest

import deadman.orchestration.handoff as handoff_module
from deadman.orchestration.handoff import (
    HANDOFF_FALLBACK_TRUNCATE,
    HandoffContext,
    HandoffManager,
)


# =====================================================================
# Mock LLM Client
# =====================================================================


class MockLLMClient:
    """模拟 LLM - 可定制 chat / chat_json 返回值"""

    def __init__(
        self,
        chat_response: str = "mock-chat",
        chat_json_resp: dict[str, Any] | None = None,
        api_key: str = "test-key",
        raise_on_chat: bool = False,
    ):
        self.api_key = api_key
        self._chat_response = chat_response
        self._chat_json_resp = chat_json_resp or {"summary": "LLM 压缩摘要"}
        self.raise_on_chat = raise_on_chat
        self.chat_call_count = 0
        self.chat_json_call_count = 0

    async def chat(self, messages, temperature=0.3, max_tokens=4096, **kwargs):
        self.chat_call_count += 1
        if self.raise_on_chat:
            raise RuntimeError("mock LLM error")
        return self._chat_response

    async def chat_json(self, messages, temperature=0.3, **kwargs):
        self.chat_json_call_count += 1
        if self.raise_on_chat:
            raise RuntimeError("mock LLM error")
        return dict(self._chat_json_resp)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_handoff(monkeypatch):
    """每个测试默认开启 handoff feature flag"""
    monkeypatch.setattr(handoff_module, "HANDOFF_ENABLED", True)
    yield


# =====================================================================
# 1. LLM 压缩消息历史
# =====================================================================


class TestHandoffCreateWithLLMCompression:
    @pytest.mark.asyncio
    async def test_handoff_create_with_llm_compression(self):
        """LLM 压缩消息历史 - chat_json 返回摘要"""
        llm = MockLLMClient(chat_json_resp={"summary": "用户咨询遗产继承问题"})
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="death_aftercare",
            to_agent="legal_advisor",
            reason="检测到法律信号",
            message_history=["用户: 亲人去世了", "Agent: 节哀", "用户: 遗产怎么分"],
        )
        assert ctx is not None
        assert ctx.from_agent == "death_aftercare"
        assert ctx.to_agent == "legal_advisor"
        assert ctx.compressed_message == "用户咨询遗产继承问题"
        assert llm.chat_json_call_count == 1
        # transfer_id 自动生成
        assert ctx.transfer_id
        # created_at 自动填充
        assert ctx.created_at is not None


# =====================================================================
# 2. LLM 不可用降级
# =====================================================================


class TestHandoffLLMUnavailableFallback:
    @pytest.mark.asyncio
    async def test_handoff_llm_unavailable_falls_back_to_truncation(self):
        """LLM 不可用（api_key 为空）→ 降级到 [:500] 截断"""
        llm = MockLLMClient(api_key="")  # 无 key
        mgr = HandoffManager(llm_client=llm)
        long_msg = "x" * 800
        ctx = await mgr.create_handoff(
            from_agent="a",
            to_agent="b",
            reason="r",
            message_history=[long_msg],
        )
        assert ctx is not None
        # 截断到 HANDOFF_FALLBACK_TRUNCATE（默认 500）
        assert len(ctx.compressed_message) == HANDOFF_FALLBACK_TRUNCATE
        # 没有调用 LLM
        assert llm.chat_json_call_count == 0
        assert llm.chat_call_count == 0

    @pytest.mark.asyncio
    async def test_handoff_llm_exception_falls_back_to_truncation(self):
        """LLM 抛异常 → 同样降级到截断"""
        llm = MockLLMClient(raise_on_chat=True)
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="a",
            to_agent="b",
            reason="r",
            message_history=["msg1", "msg2"],
        )
        assert ctx is not None
        # 降级到截断（拼接后的前 500 字符）
        assert ctx.compressed_message == "msg1\nmsg2"


# =====================================================================
# 3. 白名单过滤
# =====================================================================


class TestHandoffFilterRulesWhitelist:
    @pytest.mark.asyncio
    async def test_handoff_filter_rules_whitelist(self):
        """白名单 allow:KEY → 仅传递白名单中的 key"""
        llm = MockLLMClient()
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="a",
            to_agent="b",
            reason="r",
            message_history=["m"],
            context_vars={
                "location": "北京",
                "user_name": "张三",
                "api_key": "secret",
            },
            filter_rules=["allow:location", "allow:user_name"],
        )
        assert ctx is not None
        assert "location" in ctx.context_variables
        assert "user_name" in ctx.context_variables
        # api_key 不在白名单 → 不传递
        assert "api_key" not in ctx.context_variables


# =====================================================================
# 4. 黑名单过滤
# =====================================================================


class TestHandoffFilterRulesBlacklist:
    @pytest.mark.asyncio
    async def test_handoff_filter_rules_blacklist(self):
        """黑名单 deny:KEY → 排除指定 key，其余传递"""
        llm = MockLLMClient()
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="a",
            to_agent="b",
            reason="r",
            message_history=["m"],
            context_vars={
                "location": "北京",
                "user_name": "张三",
                "api_key": "secret",
            },
            filter_rules=["deny:api_key"],
        )
        assert ctx is not None
        assert "location" in ctx.context_variables
        assert "user_name" in ctx.context_variables
        # api_key 被黑名单排除
        assert "api_key" not in ctx.context_variables

    def test_parse_filter_rules_supports_multiple_prefixes(self):
        """_parse_filter_rules 支持 allow:/whitelist:/+ 和 deny:/blacklist:/-"""
        allow, deny = HandoffManager._parse_filter_rules(
            ["allow:a", "whitelist:b", "+c", "deny:x", "blacklist:y", "-z"]
        )
        assert allow == {"a", "b", "c"}
        assert deny == {"x", "y", "z"}


# =====================================================================
# 5. 目标 agent 应用 handoff
# =====================================================================


class TestHandoffApplyToTarget:
    @pytest.mark.asyncio
    async def test_handoff_apply_to_target(self):
        """apply_handoff 把 compressed_message 和 context_vars 注入到目标 state"""
        llm = MockLLMClient(chat_json_resp={"summary": "摘要内容"})
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="a",
            to_agent="b",
            reason="r",
            message_history=["history"],
            context_vars={"location": "上海", "pending_items": ["item1"]},
        )
        target_state: dict[str, Any] = {
            "draft_response": "",
            "user_profile": {},
            "metrics": {},
        }
        mgr.apply_handoff(ctx, target_state)
        # compressed_message 注入到 draft_response 前缀
        assert "摘要内容" in target_state["draft_response"]
        assert "来自 a 的交接上下文" in target_state["draft_response"]
        # context_variables 合并到 user_profile
        assert target_state["user_profile"]["location"] == "上海"
        assert target_state["user_profile"]["pending_items"] == ["item1"]
        # metrics 记录 handoff 元数据
        assert target_state["metrics"]["handoff_from"] == "a"
        assert target_state["metrics"]["handoff_to"] == "b"
        assert target_state["metrics"]["handoff_transfer_id"] == ctx.transfer_id

    def test_apply_handoff_does_not_overwrite_existing_response(self):
        """目标 state 已有 draft_response 时不覆盖"""
        ctx = HandoffContext(
            from_agent="a",
            to_agent="b",
            reason="r",
            compressed_message="压缩摘要",
        )
        target_state = {"draft_response": "已有响应", "user_profile": {}, "metrics": {}}
        mgr = HandoffManager()
        mgr.apply_handoff(ctx, target_state)
        # 不覆盖已有响应
        assert target_state["draft_response"] == "已有响应"

    def test_apply_handoff_none_is_noop(self):
        """handoff=None 时无副作用"""
        target_state = {"draft_response": "x", "user_profile": {}, "metrics": {}}
        mgr = HandoffManager()
        mgr.apply_handoff(None, target_state)
        assert target_state == {"draft_response": "x", "user_profile": {}, "metrics": {}}


# =====================================================================
# 6. feature flag 关闭
# =====================================================================


class TestHandoffDisabledNoChange:
    @pytest.mark.asyncio
    async def test_handoff_disabled_no_change(self, monkeypatch):
        """feature flag 关闭 → create_handoff 返回 None，调用方走旧路径"""
        monkeypatch.setattr(handoff_module, "HANDOFF_ENABLED", False)
        llm = MockLLMClient()
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="a",
            to_agent="b",
            reason="r",
            message_history=["m"],
        )
        assert ctx is None
        # LLM 未被调用
        assert llm.chat_json_call_count == 0
        assert llm.chat_call_count == 0


# =====================================================================
# 7. context vars 跨 agent 传递
# =====================================================================


class TestHandoffContextVariablesPassed:
    @pytest.mark.asyncio
    async def test_handoff_context_variables_passed(self):
        """完整链路：create_handoff → apply_handoff 后 context vars 出现在目标 state"""
        llm = MockLLMClient(chat_json_resp={"summary": "压缩摘要"})
        mgr = HandoffManager(llm_client=llm)
        ctx = await mgr.create_handoff(
            from_agent="death_aftercare",
            to_agent="legal_advisor",
            reason="转交法律",
            message_history=["msg1", "msg2"],
            context_vars={
                "user_situation": "用户父亲去世，留有房产",
                "current_question": "房产如何继承",
                "completed_items": ["死亡证明"],
            },
        )
        target_state: dict[str, Any] = {
            "draft_response": "",
            "user_profile": {"existing": "preserved"},
            "metrics": {},
        }
        mgr.apply_handoff(ctx, target_state)
        # 三个 context var 都传递到 user_profile
        assert target_state["user_profile"]["user_situation"] == "用户父亲去世，留有房产"
        assert target_state["user_profile"]["current_question"] == "房产如何继承"
        assert target_state["user_profile"]["completed_items"] == ["死亡证明"]
        # 已有 key 不被覆盖
        assert target_state["user_profile"]["existing"] == "preserved"
