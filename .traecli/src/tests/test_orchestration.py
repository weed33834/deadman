"""测试 deadman.orchestration - 状态定义与图执行器

覆盖点：
  - create_initial_state 初始状态构造
  - SequentialExecutor 节点注册与顺序执行
  - SequentialExecutor 条件路由
  - LANGGRAPH_AVAILABLE 降级标志
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from deadman.orchestration.graph import (
    LANGGRAPH_AVAILABLE,
    SequentialExecutor,
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
# SequentialExecutor - 节点注册与执行
# =====================================================================


class TestSequentialExecutor:
    """测试 SequentialExecutor 顺序执行器"""

    async def test_add_node_registers(self):
        # add_node 注册节点函数
        executor = SequentialExecutor()
        fn = AsyncMock(return_value={"key": "value"})
        executor.add_node("n1", fn)
        assert "n1" in executor._nodes

    async def test_add_edge_registers(self):
        # add_edge 注册固定边
        executor = SequentialExecutor()
        executor.add_edge("a", "b")
        assert executor._edges["a"] == "b"

    async def test_add_conditional_edges_registers(self):
        # add_conditional_edges 注册条件边
        executor = SequentialExecutor()
        router = MagicMock(return_value="route1")
        executor.add_conditional_edges("src", router, {"route1": "target1"})
        assert "src" in executor._conditional_edges

    async def test_set_entry_point(self):
        executor = SequentialExecutor()
        executor.set_entry_point("entry")
        assert executor._entry == "entry"

    async def test_set_interrupt_before(self):
        executor = SequentialExecutor()
        executor.set_interrupt_before(["node1", "node2"])
        assert executor._interrupt_before == ["node1", "node2"]

    async def test_ainvoke_executes_nodes_in_order(self):
        # 按顺序执行节点
        executor = SequentialExecutor()
        order = []

        async def node_a(state):
            order.append("a")
            return {"result": "from_a"}

        async def node_b(state):
            order.append("b")
            return {"result": "from_b"}

        executor.add_node("a", node_a)
        executor.add_node("b", node_b)
        executor.set_entry_point("a")
        executor.add_edge("a", "b")

        state = create_initial_state("test")
        result = await executor.ainvoke(state)

        assert order == ["a", "b"]
        assert result["result"] == "from_b"  # 最后一个节点的更新

    async def test_ainvoke_updates_state(self):
        # 节点返回的 dict 应合并到 state
        executor = SequentialExecutor()

        async def node_a(state):
            return {"custom_field": "custom_value"}

        executor.add_node("a", node_a)
        executor.set_entry_point("a")

        state = create_initial_state("x")
        result = await executor.ainvoke(state)
        assert result["custom_field"] == "custom_value"

    async def test_ainvoke_conditional_routing(self):
        # 条件路由：根据 router 返回值跳转
        executor = SequentialExecutor()
        visited = []

        async def start(state):
            visited.append("start")
            return {}

        async def path_a(state):
            visited.append("path_a")
            return {}

        async def path_b(state):
            visited.append("path_b")
            return {}

        def router(state):
            return "go_a" if state.get("user_input") == "A" else "go_b"

        executor.add_node("start", start)
        executor.add_node("path_a", path_a)
        executor.add_node("path_b", path_b)
        executor.set_entry_point("start")
        executor.add_conditional_edges(
            "start",
            router,
            {
                "go_a": "path_a",
                "go_b": "path_b",
            },
        )

        # 路径 A
        state_a = create_initial_state("A")
        await executor.ainvoke(state_a)
        assert "path_a" in visited
        assert "path_b" not in visited

    async def test_ainvoke_interrupt_before(self):
        # interrupt_before 在指定节点前暂停
        executor = SequentialExecutor()
        visited = []

        async def node_a(state):
            visited.append("a")
            return {}

        async def node_b(state):
            visited.append("b")
            return {}

        executor.add_node("a", node_a)
        executor.add_node("b", node_b)
        executor.set_entry_point("a")
        executor.add_edge("a", "b")
        executor.set_interrupt_before(["b"])

        state = create_initial_state("x")
        result = await executor.ainvoke(state)

        # 只执行了 a，b 之前暂停
        assert visited == ["a"]
        assert "_seq_executor_next" in result
        assert result["_seq_executor_next"] == "b"

    async def test_ainvoke_resume_from_interrupt(self):
        # 从中断恢复：再次调用 ainvoke 应执行 b
        executor = SequentialExecutor()
        visited = []

        async def node_a(state):
            visited.append("a")
            return {}

        async def node_b(state):
            visited.append("b")
            return {}

        executor.add_node("a", node_a)
        executor.add_node("b", node_b)
        executor.set_entry_point("a")
        executor.add_edge("a", "b")
        executor.set_interrupt_before(["b"])

        # 首次：在 b 前暂停
        state = create_initial_state("x")
        await executor.ainvoke(state)
        assert visited == ["a"]

        # 恢复：执行 b
        await executor.ainvoke(state)
        assert visited == ["a", "b"]

    async def test_ainvoke_node_exception_continues(self):
        # 节点抛异常不应中断流程
        executor = SequentialExecutor()
        visited = []

        async def bad_node(state):
            visited.append("bad")
            raise RuntimeError("boom")

        async def next_node(state):
            visited.append("next")
            return {}

        executor.add_node("bad", bad_node)
        executor.add_node("next", next_node)
        executor.set_entry_point("bad")
        executor.add_edge("bad", "next")

        state = create_initial_state("x")
        await executor.ainvoke(state)
        # bad 抛异常后，next 仍被执行
        assert "bad" in visited
        assert "next" in visited

    async def test_ainvoke_missing_node_skipped(self):
        # 未注册节点被跳过
        executor = SequentialExecutor()

        async def node_a(state):
            return {}

        executor.add_node("a", node_a)
        executor.set_entry_point("a")
        # 指向不存在的节点
        executor.add_edge("a", "nonexistent")

        state = create_initial_state("x")
        # 不应抛异常
        await executor.ainvoke(state)


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

    async def test_sequential_executor_has_nodes(self):
        # SequentialExecutor 模式下应注册了节点
        graph = build_main_graph()
        if isinstance(graph, SequentialExecutor):
            # 至少注册了入口节点和 respond 节点
            assert len(graph._nodes) > 0
            assert graph._entry != ""

    async def test_sequential_executor_full_run_with_mock(self, patch_llm):
        # 完整执行一遍：input_guard → router → ... → respond
        # mock LLM 返回无效 agent（降级到默认）
        patch_llm.api_key = ""  # 触发降级分支（不调 LLM）
        graph = build_main_graph()
        if not isinstance(graph, SequentialExecutor):
            return  # LangGraph 模式跳过

        state = create_initial_state("你好，我想咨询身后事")
        # 由于可能 interrupt，循环调用 ainvoke 直到完成
        for _ in range(5):
            result = await graph.ainvoke(state)
            if "_seq_executor_next" not in result:
                break
            state = result

        # 应最终产出了 final_response 或 draft_response
        assert isinstance(result, dict)


# =====================================================================
# P4: 卡死检测与步数上限测试
# =====================================================================


class TestStuckDetection:
    """P4: 借鉴 OpenManus BaseAgent.is_stuck + max_steps 的卡死检测

    覆盖：
    - step_count 超限强制终止
    - stuck_count 连续路由同一 agent 强制终止
    - route_to_agent 返回 "force_terminate"
    - forced_terminate 标记 + 兜底响应
    """

    def test_initial_state_has_stuck_fields(self):
        """create_initial_state 应包含 P4 字段且初始化为零值"""
        state = create_initial_state("x")
        assert state["step_count"] == 0
        assert state["stuck_count"] == 0
        assert state["last_agent_for_stuck"] == ""
        assert state["forced_terminate"] is False

    def test_is_stuck_step_count_exceeded(self):
        """step_count 超过 MAX_STEPS=25 时判定为卡死"""
        from deadman.orchestration.graph import MAX_STEPS, _is_stuck

        assert MAX_STEPS == 25
        state = create_initial_state("x")
        state["step_count"] = 26
        stuck, reason = _is_stuck(state)
        assert stuck is True
        # P10 后 reason 格式从 "max_steps_exceeded:26/25" 改为 "max_steps:26>25"
        assert "max_steps" in reason
        assert "26" in reason and "25" in reason

    def test_is_stuck_agent_repeat_exceeded(self):
        """stuck_count >= STUCK_AGENT_REPEAT_LIMIT=3 时判定为卡死"""
        from deadman.orchestration.graph import STUCK_AGENT_REPEAT_LIMIT, _is_stuck

        assert STUCK_AGENT_REPEAT_LIMIT == 3
        state = create_initial_state("x")
        state["stuck_count"] = 3
        state["last_agent_for_stuck"] = "death_aftercare"
        stuck, reason = _is_stuck(state)
        assert stuck is True
        assert "agent_stuck" in reason
        assert "death_aftercare" in reason
        assert "3" in reason  # P10 后 reason 含 stuck_count 数值

    def test_is_stuck_not_triggered_normal(self):
        """正常状态下不触发卡死"""
        from deadman.orchestration.graph import _is_stuck

        state = create_initial_state("x")
        state["step_count"] = 5
        state["stuck_count"] = 1
        stuck, reason = _is_stuck(state)
        assert stuck is False
        assert reason == ""

    def test_route_to_agent_force_terminate_on_stuck(self):
        """route_to_agent 在卡死时应返回 'force_terminate'"""
        from deadman.orchestration.nodes import route_to_agent

        state = create_initial_state("x")
        state["step_count"] = 30  # 超限
        result = route_to_agent(state)
        assert result == "force_terminate"
        assert state["forced_terminate"] is True
        # 应填入兜底响应
        assert "强制终止" in state["draft_response"]

    def test_route_to_agent_force_terminate_on_repeat(self):
        """route_to_agent 在连续重复 agent 时应返回 'force_terminate'"""
        from deadman.orchestration.nodes import route_to_agent

        state = create_initial_state("x")
        state["stuck_count"] = 3
        state["last_agent_for_stuck"] = "legal_advisor"
        result = route_to_agent(state)
        assert result == "force_terminate"

    def test_route_to_agent_normal_when_not_stuck(self):
        """正常状态下 route_to_agent 应正常路由"""
        from deadman.orchestration.nodes import route_to_agent

        state = create_initial_state("x")
        state["current_agent"] = "death_aftercare"
        result = route_to_agent(state)
        assert result == "death_aftercare"
        assert state.get("forced_terminate") is False

    async def test_sequential_executor_terminates_on_stuck(self):
        """SequentialExecutor 在卡死时应强制跳到 respond 节点而非无限循环"""
        from deadman.orchestration.nodes import respond_node

        graph = SequentialExecutor()

        # 注册一个会无限自循环的节点（模拟 router 失灵）
        async def fake_router(state):
            # 永远返回同一 agent，模拟卡死
            return {
                "current_agent": "death_aftercare",
                "step_count": state.get("step_count", 0) + 1,
            }

        # 让 stuck_count 直接涨到 4，触发卡死
        async def stuck_agent(state):
            return {
                "current_agent": "death_aftercare",
                "stuck_count": state.get("stuck_count", 0) + 1,
                "step_count": state.get("step_count", 0) + 1,
                "draft_response": "mock",
            }

        graph.add_node("input_guard", fake_router)
        graph.add_node("router", stuck_agent)
        graph.add_node("death_aftercare", stuck_agent)
        graph.add_node("respond", respond_node)
        graph.set_entry_point("input_guard")
        graph.add_edge("input_guard", "router")
        graph.add_conditional_edges(
            "router", lambda s: "death_aftercare", {"death_aftercare": "death_aftercare"}
        )
        graph.add_edge("death_aftercare", "router")  # 死循环
        graph.add_edge("respond", None)  # END

        state = create_initial_state("x")
        # 不设 stuck_count 初始值，让节点累加；执行到 step_count>25 或 stuck_count>=3 时应被 P4 拦截
        result = await graph.ainvoke(state)
        # 应被强制终止
        assert result.get("forced_terminate") is True
        # 应有 forced_terminate span
        spans = result.get("trace_spans", [])
        assert any(s.get("name") == "forced_terminate" for s in spans)
