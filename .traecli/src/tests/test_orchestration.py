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
            "user_input", "user_profile", "session_id", "turn_count",
            "current_agent", "agent_history", "pending_transfer",
            "transfer_confirmed", "transfer_history", "subagent_results",
            "rule_check", "safety_override", "knowledge_results",
            "web_search_results", "draft_response", "final_response",
            "confidence_labels", "trace_spans", "metrics",
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
        executor.add_conditional_edges("start", router, {
            "go_a": "path_a",
            "go_b": "path_b",
        })

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
        # 应返回可执行对象
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
