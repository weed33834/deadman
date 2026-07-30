"""主 Graph 构建 - LangGraph StateGraph + 降级顺序执行器

LangGraph 是可选依赖：
- 若 langgraph 可用，用 StateGraph 构建完整的状态图，支持条件路由、
  interrupt_before、checkpointer 等高级特性。
- 若 langgraph 不可用，降级为 SequentialExecutor，按节点顺序执行，
  支持条件路由和 interrupt_before 的基本语义。

图结构（对应 LangGraph-Orchestration.md）：

    input_guard → router → conditional(route_to_agent) → {agent nodes, user_confirm}
    user_confirm → conditional(after_user_confirm) → {router, respond}
    agents → rule_check
    rule_check → conditional(after_rule_check) → {respond, integrity_check, output_guard}
    integrity_check → output_guard
    output_guard → respond
    respond → END
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

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
from .state import ConversationState
from .termination import TerminationCondition, default_termination

logger = logging.getLogger(__name__)

# =====================================================================
# LangGraph 可选依赖探测
# =====================================================================

LANGGRAPH_AVAILABLE = False
StateGraph = None  # type: ignore
END = None  # type: ignore
MemorySaver = None  # type: ignore
SqliteSaver = None  # type: ignore
AsyncSqliteSaver = None  # type: ignore

try:
    from langgraph.checkpoint.memory import MemorySaver as _MemorySaver  # type: ignore
    from langgraph.graph import END as _END
    from langgraph.graph import StateGraph as _StateGraph  # type: ignore

    StateGraph = _StateGraph
    END = _END
    MemorySaver = _MemorySaver
    LANGGRAPH_AVAILABLE = True
    # SqliteSaver 是独立可选依赖（langgraph-checkpoint-sqlite）
    # 优先使用 AsyncSqliteSaver：web/api_chat 走 await graph.ainvoke() 异步路径，
    # 同步 SqliteSaver 不支持 async 方法（NotImplementedError）。
    try:
        from langgraph.checkpoint.sqlite.aio import (  # type: ignore[import-not-found]
            AsyncSqliteSaver as _AsyncSqliteSaver,
        )

        AsyncSqliteSaver = _AsyncSqliteSaver
    except ImportError:
        logger.info(
            "langgraph-checkpoint-sqlite[aio] 不可用，"
            "异步 checkpointer 降级为 MemorySaver。"
            "pip install langgraph-checkpoint-sqlite aiosqlite 后可获得持久化 checkpoint。"
        )
    # 同步 SqliteSaver 仅用于 CLI 同步路径
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver  # type: ignore

        SqliteSaver = _SqliteSaver
    except ImportError:
        pass
except ImportError:
    logger.info(
        "langgraph 不可用，编排将降级为 SequentialExecutor（顺序执行）模式。"
        "安装 langgraph 后可获得完整 StateGraph 能力（条件路由/checkpoint/streaming）。"
    )

# 节点名常量（与图中的 node key 对应）
NODE_INPUT_GUARD = "input_guard"
NODE_ROUTER = "router"
NODE_USER_CONFIRM = "user_confirm_node"
NODE_RULE_CHECK = "rule_check"
NODE_INTEGRITY_CHECK = "integrity_check"
NODE_OUTPUT_GUARD = "output_guard"
NODE_RESPOND = "respond"

# 路由返回值常量
ROUTE_AWAIT_TRANSFER = "await_transfer_confirm"
ROUTE_SAFETY_OVERRIDE = "safety_override"
ROUTE_NEEDS_INTEGRITY = "needs_integrity_check"
ROUTE_PASS_THROUGH = "pass_through"
ROUTE_PROCEED_TRANSFER = "proceed_transfer"
ROUTE_DECLINE_TRANSFER = "decline_transfer"
# P4: 卡死或步数超限时强制路由到 respond
ROUTE_FORCE_TERMINATE = "force_terminate"

# interrupt_before 配置 - 转介确认前暂停
INTERRUPT_BEFORE_NODES = [NODE_USER_CONFIRM]

# =====================================================================
# P4: 卡死检测与步数上限（借鉴 OpenManus BaseAgent.is_stuck + max_steps）
# =====================================================================
# 步数硬上限：单轮对话最多经过 25 个节点（input_guard + router + 6 agents *
# 最多 3 次转介 + rule_check + integrity + output_guard + respond ≈ 25）
# 触发后强制跳到 respond 节点输出兜底响应，避免死循环烧 token
MAX_STEPS = 25
# 连续路由到同一 agent 的次数上限：超过此值判定为"卡死"
# OpenManus 默认 3 次，deadman 转 6 个 agent 之间可能正常连续路由 1-2 次，
# 设 3 次是保守阈值（连续 3 次同一 agent 几乎肯定是 router 失灵）
STUCK_AGENT_REPEAT_LIMIT = 3

# P10：模块级默认终止条件单例（无状态，可复用）
# 等价于 MaxStepsTermination(MAX_STEPS) | StuckAgentTermination(STUCK_AGENT_REPEAT_LIMIT)
_default_termination: TerminationCondition = default_termination()


def _is_stuck(state: ConversationState) -> tuple[bool, str]:
    """卡死检测 - 委托给 default_termination()，保留原签名以向后兼容

    P10 后内部走可组合终止条件（借鉴 AutoGen TerminationCondition）：
    默认等价于 MaxStepsTermination(MAX_STEPS) | StuckAgentTermination(STUCK_AGENT_REPEAT_LIMIT)。
    行为与原 P4 实现完全一致，只是把硬编码逻辑抽到 termination.py 便于组合扩展。

    判定条件（任一满足即卡死）：
    1. step_count > MAX_STEPS：节点执行数超限
    2. stuck_count >= STUCK_AGENT_REPEAT_LIMIT：连续多次路由到同一 agent

    Returns:
        (is_stuck, reason) - reason 在 is_stuck=True 时为卡死原因
    """
    result = _default_termination.evaluate(state)
    return result.should_terminate, result.reason


def _increment_step(state: ConversationState) -> None:
    """递增 step_count 并更新 stuck_count（在节点执行后调用）

    stuck_count 逻辑：
    - 若当前 agent == last_agent_for_stuck：stuck_count += 1
    - 否则：stuck_count = 1（重置但记录新 agent）
    """
    step_count = state.get("step_count", 0) + 1
    state["step_count"] = step_count
    current = state.get("current_agent", "")
    last = state.get("last_agent_for_stuck", "")
    if current and current == last:
        state["stuck_count"] = state.get("stuck_count", 0) + 1
    elif current:
        state["stuck_count"] = 1
        state["last_agent_for_stuck"] = current


# =====================================================================
# SequentialExecutor - 降级模式顺序执行器
# =====================================================================


class SequentialExecutor:
    """降级模式顺序执行器 - 当 LangGraph 不可用时使用

    模拟 LangGraph StateGraph 的核心行为：
    - 按节点顺序执行
    - 支持固定边（add_edge）和条件边（add_conditional_edges）
    - 支持 interrupt_before（在指定节点前暂停，等待用户输入后恢复）
    - 支持 ainvoke 异步调用

    不支持的高级特性：checkpointer、streaming、subgraph、时间旅行。
    """

    def __init__(self, termination: TerminationCondition | None = None) -> None:
        """初始化顺序执行器

        Args:
            termination: 可选的终止条件。None 时用 default_termination()
                （等价 P4 的 MAX_STEPS + STUCK_AGENT_REPEAT_LIMIT）。
                可传入自定义组合条件，如：
                default_termination() | TokenUsageTermination(50_000)
        """
        self._nodes: dict[str, Callable[[ConversationState], Awaitable[dict[str, Any]]]] = {}
        self._edges: dict[str, str] = {}  # 固定边：source -> target
        self._conditional_edges: dict[
            str, tuple[Callable[[ConversationState], str], dict[str, str]]
        ] = {}
        self._entry: str = ""
        self._interrupt_before: list[str] = []
        # P10：可注入的终止条件（默认等价 P4 行为）
        self._termination: TerminationCondition = termination or _default_termination

    def add_node(
        self, name: str, fn: Callable[[ConversationState], Awaitable[dict[str, Any]]]
    ) -> None:
        """注册一个节点"""
        self._nodes[name] = fn

    def add_edge(self, source: str, target: str) -> None:
        """添加固定边：source 执行完后无条件跳到 target"""
        self._edges[source] = target

    def add_conditional_edges(
        self,
        source: str,
        router_fn: Callable[[ConversationState], str],
        mapping: dict[str, str],
    ) -> None:
        """添加条件边：source 执行完后调用 router_fn，按返回值映射到下一节点"""
        self._conditional_edges[source] = (router_fn, mapping)

    def set_entry_point(self, name: str) -> None:
        """设置入口节点"""
        self._entry = name

    def set_interrupt_before(self, nodes: list[str]) -> None:
        """设置在哪些节点前暂停"""
        self._interrupt_before = list(nodes)

    async def ainvoke(
        self,
        state: ConversationState,
        config: dict[str, Any] | None = None,
    ) -> ConversationState:
        """异步执行图

        从入口节点开始，按边顺序执行。遇到 interrupt_before 节点时暂停并返回当前状态。
        调用方可在更新状态后再次调用 ainvoke 恢复执行。

        Args:
            state: 初始/恢复状态
            config: 可选配置（与 LangGraph 兼容，当前未使用）

        Returns:
            执行完成或暂停时的最终状态
        """
        # 检查是否从中断恢复（state 中有 _seq_executor_next 标记）
        next_node = state.get("_seq_executor_next", self._entry)  # type: ignore[typeddict-item]
        resuming = "_seq_executor_next" in state  # 是否为恢复执行
        current: str | None = next_node if isinstance(next_node, str) else self._entry
        # 恢复时清除标记，避免下次再次触发同一节点的 interrupt
        if resuming:
            state.pop("_seq_executor_next", None)  # type: ignore[typeddict-item]

        while current and current != END:
            # interrupt_before：在执行节点前暂停（恢复执行的首个节点跳过此检查）
            if not resuming and current in self._interrupt_before:
                # 标记恢复点，调用方可通过再次调用 ainvoke 恢复
                state["_seq_executor_next"] = current  # type: ignore[typeddict-item]
                logger.info("SequentialExecutor 在节点 %s 前暂停（interrupt_before）", current)
                return state
            # 后续节点正常检查 interrupt
            resuming = False

            # P4: 卡死检测 - 执行节点前检查是否已卡死
            stuck, stuck_reason = _is_stuck(state)
            if stuck and current != NODE_RESPOND:
                logger.warning(
                    "SequentialExecutor 检测到卡死 reason=%s，强制跳转到 respond 节点",
                    stuck_reason,
                )
                state["forced_terminate"] = True
                # 兜底响应：若 draft_response 为空则填入
                if not state.get("draft_response"):
                    state["draft_response"] = (
                        "抱歉，系统在处理您的请求时检测到循环或超限，"
                        "已强制终止本轮处理。请尝试重新表述您的问题，"
                        "或拆分为更具体的小问题逐个询问。"
                    )
                # 追加 trace span 便于排查
                spans = state.get("trace_spans", [])
                spans.append(
                    {
                        "span_type": "system",
                        "name": "forced_terminate",
                        "attributes": {
                            "reason": stuck_reason,
                            "step_count": state.get("step_count", 0),
                        },
                    }
                )
                state["trace_spans"] = spans
                current = NODE_RESPOND
                continue

            # 执行节点
            node_fn = self._nodes.get(current)
            if node_fn is not None:
                try:
                    result = await node_fn(state)
                    if isinstance(result, dict):
                        state.update(dict(result))  # type: ignore[typeddict-item]
                except Exception as e:
                    logger.error("节点 %s 执行异常: %s", current, e, exc_info=True)
                    # 异常不中断流程，继续到下一节点
            else:
                logger.warning("节点 %s 未注册，跳过", current)

            # P4: 节点执行后递增 step_count + 更新 stuck_count
            _increment_step(state)

            # 确定下一节点
            if current in self._conditional_edges:
                router_fn, mapping = self._conditional_edges[current]
                try:
                    route_key = router_fn(state)
                except Exception as e:
                    logger.error("路由函数 %s 执行异常: %s", current, e, exc_info=True)
                    route_key = ""
                current = mapping.get(route_key, END)
            elif current in self._edges:
                current = self._edges[current]
            else:
                # 没有出边，结束
                current = END  # type: ignore[assignment]

        # 清理恢复标记
        state.pop("_seq_executor_next", None)  # type: ignore[typeddict-item]
        return state

    def invoke(
        self, state: ConversationState, config: dict[str, Any] | None = None
    ) -> ConversationState:
        """同步执行图（包装 ainvoke，用 asyncio.run）

        便捷方法，内部调用 asyncio.run(self.ainvoke(state, config))。
        不应在已有事件循环的环境中使用（会抛 RuntimeError）。
        """
        import asyncio

        return asyncio.run(self.ainvoke(state, config))


# =====================================================================
# build_main_graph - 构建主编排图
# =====================================================================


def _build_sequential_executor() -> SequentialExecutor:
    """构建 SequentialExecutor（降级模式）

    按照与 LangGraph 版本相同的图结构注册节点和边。
    """
    executor = SequentialExecutor()

    # === 注册节点 ===
    executor.add_node(NODE_INPUT_GUARD, input_guard_node)
    executor.add_node(NODE_ROUTER, router_node)
    executor.add_node(NODE_USER_CONFIRM, user_confirm_node)
    # 6 个智能体节点都使用通用的 agent_node
    for agent_name in AGENT_NAMES:
        executor.add_node(agent_name, agent_node)
    executor.add_node(NODE_RULE_CHECK, rule_check_node)
    executor.add_node(NODE_INTEGRITY_CHECK, integrity_check_node)
    executor.add_node(NODE_OUTPUT_GUARD, output_guard_node)
    executor.add_node(NODE_RESPOND, respond_node)

    # === 入口边 ===
    executor.set_entry_point(NODE_INPUT_GUARD)
    executor.add_edge(NODE_INPUT_GUARD, NODE_ROUTER)

    # === 路由边（条件） - router 之后 ===
    route_mapping: dict[str, str] = {name: name for name in AGENT_NAMES}
    route_mapping[ROUTE_AWAIT_TRANSFER] = NODE_USER_CONFIRM
    route_mapping[ROUTE_FORCE_TERMINATE] = NODE_RESPOND  # P4: 卡死时直接跳 respond
    executor.add_conditional_edges(NODE_ROUTER, route_to_agent, route_mapping)

    # === 用户确认转介后 ===
    executor.add_conditional_edges(
        NODE_USER_CONFIRM,
        after_user_confirm,
        {
            ROUTE_PROCEED_TRANSFER: NODE_ROUTER,  # 用户同意，回到 router 路由到目标智能体
            ROUTE_DECLINE_TRANSFER: NODE_RESPOND,  # 用户拒绝，直接响应
        },
    )

    # === 智能体执行后 → 规则校验 ===
    for agent_name in AGENT_NAMES:
        executor.add_edge(agent_name, NODE_RULE_CHECK)

    # === 规则校验后 ===
    executor.add_conditional_edges(
        NODE_RULE_CHECK,
        after_rule_check,
        {
            ROUTE_SAFETY_OVERRIDE: NODE_RESPOND,  # L0 触发，直接响应
            ROUTE_NEEDS_INTEGRITY: NODE_INTEGRITY_CHECK,  # 需要 5 关事实复核
            ROUTE_PASS_THROUGH: NODE_OUTPUT_GUARD,  # 直接通过
        },
    )

    # === 事实复核 → 输出校验 → 响应 ===
    executor.add_edge(NODE_INTEGRITY_CHECK, NODE_OUTPUT_GUARD)
    executor.add_edge(NODE_OUTPUT_GUARD, NODE_RESPOND)

    # === 响应 → 结束 ===
    executor.add_edge(NODE_RESPOND, END)  # type: ignore[arg-type]

    # === interrupt_before 配置 ===
    executor.set_interrupt_before(INTERRUPT_BEFORE_NODES)

    return executor


def _build_langgraph():
    """构建 LangGraph StateGraph（完整模式）

    返回 langgraph.compile() 后的可执行图，支持 ainvoke / invoke / stream。
    """
    graph = StateGraph(ConversationState)  # type: ignore[misc]

    # === 注册节点 ===
    graph.add_node(NODE_INPUT_GUARD, input_guard_node)
    graph.add_node(NODE_ROUTER, router_node)
    graph.add_node(NODE_USER_CONFIRM, user_confirm_node)
    # 6 个智能体节点都使用通用的 agent_node
    for agent_name in AGENT_NAMES:
        graph.add_node(agent_name, agent_node)
    graph.add_node(NODE_RULE_CHECK, rule_check_node)
    graph.add_node(NODE_INTEGRITY_CHECK, integrity_check_node)
    graph.add_node(NODE_OUTPUT_GUARD, output_guard_node)
    graph.add_node(NODE_RESPOND, respond_node)

    # === 入口边 ===
    graph.set_entry_point(NODE_INPUT_GUARD)
    graph.add_edge(NODE_INPUT_GUARD, NODE_ROUTER)

    # === 路由边（条件） - router 之后 ===
    route_mapping: dict[str, str] = {name: name for name in AGENT_NAMES}
    route_mapping[ROUTE_AWAIT_TRANSFER] = NODE_USER_CONFIRM
    route_mapping[ROUTE_FORCE_TERMINATE] = NODE_RESPOND  # P4: 卡死时直接跳 respond
    graph.add_conditional_edges(NODE_ROUTER, route_to_agent, route_mapping)

    # === 用户确认转介后 ===
    graph.add_conditional_edges(
        NODE_USER_CONFIRM,
        after_user_confirm,
        {
            ROUTE_PROCEED_TRANSFER: NODE_ROUTER,  # 用户同意，回到 router 路由到目标智能体
            ROUTE_DECLINE_TRANSFER: NODE_RESPOND,  # 用户拒绝，直接响应
        },
    )

    # === 智能体执行后 → 规则校验 ===
    for agent_name in AGENT_NAMES:
        graph.add_edge(agent_name, NODE_RULE_CHECK)

    # === 规则校验后 ===
    graph.add_conditional_edges(
        NODE_RULE_CHECK,
        after_rule_check,
        {
            ROUTE_SAFETY_OVERRIDE: NODE_RESPOND,  # L0 触发，直接响应
            ROUTE_NEEDS_INTEGRITY: NODE_INTEGRITY_CHECK,  # 需要 5 关事实复核
            ROUTE_PASS_THROUGH: NODE_OUTPUT_GUARD,  # 直接通过
        },
    )

    # === 事实复核 → 输出校验 → 响应 ===
    graph.add_edge(NODE_INTEGRITY_CHECK, NODE_OUTPUT_GUARD)
    graph.add_edge(NODE_OUTPUT_GUARD, NODE_RESPOND)

    # === 响应 → 结束 ===
    graph.add_edge(NODE_RESPOND, END)

    # === 编译 ===
    # checkpointer 选择策略：
    #   - 当前 web 路径用 await graph.ainvoke() 异步调用，
    #     AsyncSqliteSaver 需要 aiosqlite.Connection（无法在 sync build_main_graph 内创建）；
    #     sync SqliteSaver 不支持 async API（NotImplementedError）。
    #   - 因此 web 路径用 MemorySaver（同时支持 sync/async），跨会话持久化由
    #     MemoryManager.after_turn + 文件存储独立负责（不依赖 checkpointer）。
    #   - CLI 同步路径可继续用 SqliteSaver（持久化 CLI 对话状态）。
    checkpointer = None
    if MemorySaver is not None:
        try:
            checkpointer = MemorySaver()
            logger.info(
                "checkpointer 使用 MemorySaver（兼容 sync+async，跨会话状态由 MemoryManager 负责）"
            )
        except Exception as e:
            logger.warning("MemorySaver 初始化失败，编译无 checkpointer: %s", e)

    try:
        compiled = graph.compile(
            checkpointer=checkpointer,
            interrupt_before=INTERRUPT_BEFORE_NODES,
        )
    except TypeError:
        # 某些 langgraph 版本不支持 interrupt_before 参数
        logger.warning("langgraph.compile 不支持 interrupt_before，编译无中断支持")
        compiled = graph.compile(checkpointer=checkpointer)

    return compiled


def build_main_graph():
    """构建主编排图

    - 若 langgraph 可用，返回 langgraph.compile() 后的 StateGraph
    - 若 langgraph 不可用，返回 SequentialExecutor（降级模式）

    两种模式共享相同的节点函数和路由逻辑，对上层调用方透明。
    调用方可通过 LANGGRAPH_AVAILABLE 判断当前模式。

    Returns:
        LangGraph 编译图（支持 invoke/ainvoke）或 SequentialExecutor
    """
    if LANGGRAPH_AVAILABLE:
        try:
            return _build_langgraph()
        except Exception as e:
            logger.error("LangGraph 构建失败，降级到 SequentialExecutor: %s", e, exc_info=True)
    return _build_sequential_executor()
