"""测试 deadman.orchestration - 状态定义与图构建

覆盖点：
  - create_initial_state 初始状态构造
  - build_main_graph 图构建（LangGraph StateGraph 单一实现）
  - LANGGRAPH_AVAILABLE 标志
"""

from __future__ import annotations

from deadman.orchestration.graph import (
    LANGGRAPH_AVAILABLE,
    build_main_graph,
)
from deadman.orchestration.state import ConversationState, create_initial_state

# =====================================================================
# create_initial_state
# =====================================================================


class TestCreateInitialState:
    """测试 create_initial_state 初始状态构造"""

    def test_returns_dict_with_defaults(self):
        # 返回 dict 且字段齐全
        state = create_initial_state("用户问题")
        assert isinstance(state, dict)
        assert state["user_input"] == "用户问题"
        assert state["user_profile"] == {}
        assert state["session_id"] == ""
        assert state["turn_count"] == 0

    def test_default_current_agent_empty(self):
        # 默认 current_agent 为空字符串
        state = create_initial_state("x")
        assert state["current_agent"] == ""

    def test_default_safety_override_false(self):
        # 默认 safety_override 为 False
        state = create_initial_state("x")
        assert state["safety_override"] is False

    def test_default_collections_empty(self):
        # 默认集合类字段为空
        state = create_initial_state("x")
        assert state["agent_history"] == []
        assert state["transfer_history"] == []
        assert state["subagent_results"] == []
        assert state["knowledge_results"] == []
        assert state["trace_spans"] == []

    def test_with_user_profile(self):
        # 传入 user_profile
        profile = {"location": {"city": "北京"}}
        state = create_initial_state("x", user_profile=profile)
        assert state["user_profile"] == profile

    def test_with_session_id(self):
        state = create_initial_state("x", session_id="sess-123")
        assert state["session_id"] == "sess-123"

    def test_rule_check_default_none(self):
        # 默认 rule_check 为 None
        state = create_initial_state("x")
        assert state["rule_check"] is None

    def test_pending_transfer_default_none(self):
        state = create_initial_state("x")
        assert state["pending_transfer"] is None
        assert state["transfer_confirmed"] is None

    def test_all_expected_fields_present(self):
        # 所有预期字段都存在
        state = create_initial_state("x")
        expected_fields = {
            "user_input",
            "user_profile",
            "session_id",
            "turn_count",
            "current_agent",
            "agent_history",
            "pending_transfer",
            "transfer_confirmed",
            "transfer_history",
            "subagent_results",
            "rule_check",
            "safety_override",
            "knowledge_results",
            "web_search_results",
            "draft_response",
            "final_response",
            "confidence_labels",
            "trace_spans",
            "metrics",
        }
        assert expected_fields.issubset(set(state.keys()))


# =====================================================================
# ConversationState - TypedDict
# =====================================================================


class TestConversationState:
    """测试 ConversationState TypedDict"""

    def test_can_be_constructed_as_dict(self):
        # TypedDict 本质是 dict，可直接构造
        state: ConversationState = {
            "user_input": "x",
            "session_id": "s1",
            "current_agent": "death_aftercare",
        }
        assert state["user_input"] == "x"
        assert state["current_agent"] == "death_aftercare"

    def test_total_false_allows_partial(self):
        # total=False 允许部分字段
        state: ConversationState = {"user_input": "x"}
        assert "user_input" in state
        # 不要求所有字段都存在
        assert "session_id" not in state


# =====================================================================
# build_main_graph - 降级模式
# =====================================================================


class TestBuildMainGraph:
    """测试 build_main_graph 图构建"""

    def test_returns_executor(self):
        graph = build_main_graph()
        assert graph is not None

    def test_langgraph_available_flag(self):
        # LANGGRAPH_AVAILABLE 是布尔值
        assert isinstance(LANGGRAPH_AVAILABLE, bool)


# =====================================================================
# P4: 卡死检测与步数上限测试
# =====================================================================
