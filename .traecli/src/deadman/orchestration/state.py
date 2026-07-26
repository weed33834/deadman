"""ConversationState - LangGraph 全局状态定义

跨所有节点共享的对话状态。对应 LangGraph-Orchestration.md 的状态对象设计。
使用 total=False 让所有字段可选，因为状态在节点间逐步累积。
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from ..types import RuleCheckResult, SubagentResult, TransferSummary


class ConversationState(TypedDict, total=False):
    """LangGraph 全局状态 - 跨所有节点共享

    字段分组：
        用户上下文：user_input / user_profile / session_id / turn_count
        当前智能体：current_agent / agent_history
        转介机制：pending_transfer / transfer_confirmed / transfer_history
        子智能体：subagent_results
        规则校验：rule_check / safety_override
        知识检索：knowledge_results / web_search_results
        输出：draft_response / final_response / confidence_labels
        可观测性：trace_spans / metrics
    """

    # === 用户上下文 ===
    user_input: str
    user_profile: dict[str, Any]  # 地点/关系/时间/情形/遗嘱/家庭/财产
    session_id: str
    turn_count: int

    # === 当前活跃智能体 ===
    current_agent: str  # death_aftercare / legal_advisor / ...
    agent_history: list[str]  # 本轮对话经过的智能体序列

    # === 转介机制 ===
    pending_transfer: Optional[TransferSummary]
    transfer_confirmed: Optional[bool]  # None=未询问, True=已确认, False=已拒绝
    transfer_history: list[TransferSummary]

    # === 子智能体结果 ===
    subagent_results: list[SubagentResult]

    # === 规则校验 ===
    rule_check: Optional[RuleCheckResult]
    safety_override: bool  # L0 触发时为 True，跳过其他流程

    # === 知识库检索 ===
    knowledge_results: list[dict[str, Any]]
    web_search_results: list[dict[str, Any]]

    # === 输出 ===
    draft_response: str
    final_response: str
    confidence_labels: list[dict[str, Any]]  # 置信度标注

    # === 可观测性 ===
    trace_spans: list[dict[str, Any]]  # 本轮产生的 span
    metrics: dict[str, Any]  # 本轮指标

    # === P4: 卡死检测与步数上限（借鉴 OpenManus BaseAgent.is_stuck + max_steps）===
    step_count: int  # 本轮已执行的节点数（防止无限循环）
    last_agent_for_stuck: str  # 上次路由到的 agent（用于检测连续路由到同一 agent）
    stuck_count: int  # 连续路由到同一 agent 的次数
    forced_terminate: bool  # 是否被强制终止（卡死或步数超限）

    # === P4.2: Scratchpad（按 agent_name 索引的草稿本）===
    # feature flag DEADMAN_SCRATCHPAD_ENABLED=0 默认关闭；
    # 关闭时该字段保持 {}，行为完全不变
    scratchpads: dict[str, list[str]]

    # === P4.1: Handoff 上下文（确认转介后构造的 HandoffContext 快照）===
    # feature flag DEADMAN_HANDOFF_ENABLED=0 默认关闭；
    # 关闭时该字段保持 None，行为完全不变
    handoff_context: Any

    # === P5.3: GUID 沙箱（input_guard_node 检测到外部内容时填充）===
    # feature flag DEADMAN_GUID_SANDBOX_ENABLED=0 默认关闭；
    # 关闭时这两个字段不存在，行为完全不变
    guid_sandbox_wrapped_input: str  # 用 GUID 包裹后的 user_input
    guid_sandbox_preamble: str  # 注入 system_prompt 的沙箱说明


def create_initial_state(
    user_input: str,
    user_profile: dict[str, Any] | None = None,
    session_id: str = "",
) -> ConversationState:
    """创建初始 ConversationState，所有字段预填默认值

    便于调用方快速启动一轮对话，无需手动构造全部字段。
    """
    return ConversationState(
        user_input=user_input,
        user_profile=user_profile or {},
        session_id=session_id,
        turn_count=0,
        current_agent="",
        agent_history=[],
        pending_transfer=None,
        transfer_confirmed=None,
        transfer_history=[],
        subagent_results=[],
        rule_check=None,
        safety_override=False,
        knowledge_results=[],
        web_search_results=[],
        draft_response="",
        final_response="",
        confidence_labels=[],
        trace_spans=[],
        metrics={},
        step_count=0,
        last_agent_for_stuck="",
        stuck_count=0,
        forced_terminate=False,
        scratchpads={},
        handoff_context=None,
    )
