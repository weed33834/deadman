"""主 Graph 构建 - LangGraph StateGraph（单一实现）

langgraph 是硬依赖，编排统一走 StateGraph（条件路由 / interrupt_before / checkpointer）。
历史上曾有一个手写 SequentialExecutor 模拟器作降级路径，因 langgraph 硬依赖后
永不可达且无人维护，已删除。

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
PostgresSaver = None  # type: ignore

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
    # PostgresSaver 是独立可选依赖（langgraph-checkpoint-postgres）
    # 企业级扩展④j：DATABASE_URL 配置时优先用 PostgresSaver 实现跨进程持久化 checkpoint
    try:
        from langgraph.checkpoint.postgres import PostgresSaver as _PostgresSaver  # type: ignore

        PostgresSaver = _PostgresSaver
    except ImportError:
        logger.info(
            "langgraph-checkpoint-postgres 不可用，"
            "checkpointer 降级为 MemorySaver。"
            "pip install langgraph-checkpoint-postgres psycopg 后可获得跨进程持久化 checkpoint。"
        )
except ImportError:
    logger.warning(
        "langgraph 不可用，build_main_graph() 将抛 ImportError。"
        "请执行: pip install 'langgraph>=1.0'"
    )

# PostgresSaver 全局单例（惰性初始化，保持连接引用避免 GC 关闭）
_postgres_checkpointer: Any = None
_postgres_cm: Any = None  # context manager 引用，避免 __exit__ 被提前触发

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
    """递增 step_count（在节点执行后调用）。

    stuck_count / last_agent_for_stuck 不在此处更新——由 agent_node 专门维护
    （仅当 agent 实际执行时才递增 stuck_count）。此前此处对每个节点（含
    rule_check / output_guard 等非 agent 节点）都递增 stuck_count，导致
    正常单趟流程（router→agent→rule_check→output_guard）在到达 output_guard
    前 stuck_count 就累到 3，误触发假卡死强制终止，跳过 output_guard 的
    置信度标注与 PII 掩码。修复后 stuck_count 仅在 agent 反复路由同一智能体
    时增长，符合 P4 设计意图（借鉴 OpenManus is_stuck）。
    """
    step_count = state.get("step_count", 0) + 1
    state["step_count"] = step_count


# =====================================================================
# build_main_graph - 构建主编排图
# =====================================================================


def _get_postgres_checkpointer():
    """惰性创建全局 PostgresSaver 单例（企业级扩展④j）。

    DATABASE_URL 配置且 langgraph-checkpoint-postgres 可用时，创建持久化
    checkpointer（跨进程共享 checkpoint 状态）。setup() 建表（幂等）。

    Returns:
        PostgresSaver 实例；不可用时返回 None。
    """
    global _postgres_checkpointer, _postgres_cm
    if _postgres_checkpointer is not None:
        return _postgres_checkpointer
    if PostgresSaver is None:
        return None
    try:
        import contextlib

        from ..config import settings

        db_url = settings.database_url
        if not db_url:
            return None
        # psycopg 需要 postgresql:// 协议（非 +asyncpg）
        if db_url.startswith("postgresql+asyncpg://"):
            db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        elif db_url.startswith("postgresql+psycopg://"):
            db_url = db_url.replace("postgresql+psycopg://", "postgresql://", 1)
        # from_conn_string 返回 context manager；__enter__ 创建连接并返回 checkpointer
        _postgres_cm = PostgresSaver.from_conn_string(db_url)
        _postgres_checkpointer = _postgres_cm.__enter__()
        # setup() 建表（幂等），同步方法
        _postgres_checkpointer.setup()
        logger.info("checkpointer 使用 PostgresSaver（跨进程持久化 checkpoint）")
        return _postgres_checkpointer
    except Exception as e:
        logger.warning(
            "PostgresSaver 初始化失败，降级到 MemorySaver: %s。"
            "pip install langgraph-checkpoint-postgres psycopg 后可启用。",
            e,
        )
        # 清理可能半初始化的资源
        if _postgres_cm is not None:
            with contextlib.suppress(Exception):
                _postgres_cm.__exit__(None, None, None)
        _postgres_checkpointer = None
        _postgres_cm = None
        return None


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
    # checkpointer 选择策略（企业级扩展④j 优先级）：
    #   1. PostgresSaver：DATABASE_URL 配置 + langgraph-checkpoint-postgres 可用时，
    #      跨进程持久化 checkpoint（生产推荐，多 worker 共享对话状态）。
    #   2. MemorySaver：默认降级，兼容 sync+async，跨会话状态由
    #      MemoryManager.after_turn + 文件存储独立负责。
    #   注：AsyncSqliteSaver 需要 aiosqlite.Connection（无法在 sync build_main_graph
    #       内创建）；sync SqliteSaver 不支持 async API（NotImplementedError），
    #       故 SQLite checkpointer 仅用于 CLI 同步路径，web 异步路径不用。
    checkpointer = None
    # 优先尝试 PostgresSaver
    try:
        pg_checkpointer = _get_postgres_checkpointer()
        if pg_checkpointer is not None:
            checkpointer = pg_checkpointer
    except Exception as e:
        logger.warning("PostgresSaver 获取失败，降级 MemorySaver: %s", e)
    # 降级到 MemorySaver
    if checkpointer is None and MemorySaver is not None:
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
    """构建主编排图（LangGraph StateGraph，单一实现）

    langgraph 是 pyproject 硬依赖；不可用属于环境损坏，直接抛错而非静默降级
    （历史上曾有手写 SequentialExecutor 模拟器，已删除——重复引擎无人维护）。

    Returns:
        LangGraph 编译图（支持 invoke/ainvoke/stream）
    """
    if not LANGGRAPH_AVAILABLE:
        raise ImportError(
            "langgraph 未安装或导入失败，无法构建编排图。请执行: pip install 'langgraph>=1.0'"
        )
    return _build_langgraph()


def default_graph_config(session_id: str = "") -> dict[str, Any]:
    """生成 graph.ainvoke() 所需的默认 config。

    checkpointer（MemorySaver）要求 config.configurable.thread_id，
    否则抛 ``ValueError: Checkpointer requires one or more of the following
    'configurable' keys: thread_id``。

    Args:
        session_id: 会话 ID；为空时自动生成临时 thread_id。

    Returns:
        ``{"configurable": {"thread_id": ...}}``
    """
    import uuid

    thread_id = session_id or f"deadman-thread-{uuid.uuid4().hex[:12]}"
    return {"configurable": {"thread_id": thread_id}}
