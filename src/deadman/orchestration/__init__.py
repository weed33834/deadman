"""身后事多智能体平台 - LangGraph 编排底座

本包实现基于 LangGraph 的多智能体编排层，将 agent.md 驱动的 6 并列智能体、
转介机制、规则校验链映射为可执行的 StateGraph。

核心导出：
    - ConversationState: LangGraph 全局状态 TypedDict
    - create_initial_state: 创建初始状态的便捷函数
    - build_main_graph: 构建主编排图（LangGraph StateGraph）
    - LANGGRAPH_AVAILABLE: LangGraph 是否可用
    - 各节点函数: input_guard_node / router_node / agent_node / ...
    - 各路由函数: route_to_agent / after_rule_check / after_user_confirm

使用示例：
    from deadman.orchestration import build_main_graph, create_initial_state

    graph = build_main_graph()
    state = create_initial_state(user_input="亲人去世了怎么办？")
    result = await graph.ainvoke(state)
    print(result["final_response"])
"""

from __future__ import annotations

from .graph import (
    LANGGRAPH_AVAILABLE,
    build_main_graph,
)
from .nodes import (
    AGENT_NAMES,
    after_rule_check,
    after_user_confirm,
    agent_node,
    input_guard_node,
    integrity_check_node,
    output_guard_node,
    respond_node,
    route_to_agent,
    router_node,
    rule_check_node,
    user_confirm_node,
)
from .state import ConversationState, create_initial_state

__all__ = [
    # 状态
    "ConversationState",
    "create_initial_state",
    # 图构建
    "build_main_graph",
    "LANGGRAPH_AVAILABLE",
    # 节点函数
    "input_guard_node",
    "router_node",
    "user_confirm_node",
    "agent_node",
    "rule_check_node",
    "integrity_check_node",
    "output_guard_node",
    "respond_node",
    # 路由函数
    "route_to_agent",
    "after_rule_check",
    "after_user_confirm",
    # 常量
    "AGENT_NAMES",
]
