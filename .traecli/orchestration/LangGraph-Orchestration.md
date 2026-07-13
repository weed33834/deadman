# LangGraph 编排底座

> 本文件定义如何用 LangGraph 把现有"agent.md 驱动 + 6 并列智能体 + 12 私有子智能体 + 转介机制"映射为可执行的 StateGraph。借鉴 LangGraph（LangChain 官方）、AutoGen（Microsoft）、CrewAI、AgentScope（阿里）、LlamaIndex Workflows。
>
> **重要**：本文件是参考实现。核心架构仍是 agent.md 驱动，LangGraph 只是"编排底座"的一种实现选择。TRAE / Coze / Dify / OpenAI Assistants 等平台可参照同样的映射规则用自己的可视化编排或 SDK 实现。

## 为什么需要编排底座

### 当前痛点

TEAM.md 已定义：
- 6 个并列智能体 + 12 私有子智能体
- 转介（recommend）机制
- 子智能体调用时机（硬约束）
- 规则优先级链 L0-L8

但这些都是**规则描述**，没有**可执行的运行时**：
- 转介触发后，状态怎么传递？
- 子智能体失败后，Reflexion 怎么挂载？
- 规则校验在流程的哪个节点执行？
- 多轮对话的 checkpoint 怎么管理？
- 跨智能体的 trace 怎么串联？

### LangGraph 补强

LangGraph 提供：
1. **StateGraph**：显式状态对象，跨节点传递
2. **Conditional Edges**：转介机制的自然映射
3. **Subgraphs**：子智能体的自然映射
4. **Checkpointer**：多轮对话 / 跨会话续接
5. **Streaming**：流式输出
6. **Human-in-the-loop**：用户确认转介
7. **时间旅行**：回放 / 调试

## 平台无关的映射规则（核心）

无论用什么平台实现，都遵循以下映射：

| 现有概念 | LangGraph 概念 | TRAE 对应 | Coze 对应 | Dify 对应 |
|---------|---------------|----------|----------|----------|
| 6 并列智能体 | 6 个 node | 6 个 agent.md | 6 个 Bot | 6 个 Agent 节点 |
| 私有子智能体 | subgraph | subagent | 插件调用 | 工作流子流程 |
| 转介机制 | conditional edge | 推荐 agent | 意图路由 | 条件分支节点 |
| 规则优先级链 | pre/post hook | rules/ 加载 | 知识库 + 约束 | LLM 节点前置约束 |
| 转介摘要 | state 字段 | 上下文传递 | 变量传递 | 变量 |
| 用户确认转介 | interrupt | 用户输入节点 | 用户确认卡片 | 人工审批节点 |
| Reflexion 重试 | node 内循环 | agent 自循环 | 重试插件 | 循环节点 |
| MCP 工具 | tool node | MCP 工具 | 插件 | 工具节点 |
| trace | LangGraph stream | OTel span | 日志 | 日志 |

## 状态对象设计

```python
# orchestration/state.py（伪代码）

from typing import TypedDict, Literal, Optional, Any
from pydantic import BaseModel, Field
from enum import Enum


class RiskTier(str, Enum):
    R0 = "R0"  # 常规
    R1 = "R1"  # 注意
    R2 = "R2"  # 转介
    R3 = "R3"  # 安全优先


class TransferSummary(BaseModel):
    """转介摘要 - TEAM.md 定义的 7 字段"""
    from_agent: str
    to_agent: str
    reason: str
    user_situation: str
    current_question: str
    completed_items: list[str] = Field(default_factory=list)
    pending_items: list[str] = Field(default_factory=list)


class SubagentResult(BaseModel):
    """子智能体返回结果 - TEAM.md 定义"""
    subagent_name: str
    execution_mode: Literal["success", "fallback", "failed"]
    report: dict[str, Any]
    confidence: float
    sources: list[str] = Field(default_factory=list)


class RuleCheckResult(BaseModel):
    """规则校验结果"""
    passed: bool
    violations: list[dict] = Field(default_factory=list)
    risk_tier: RiskTier = RiskTier.R0
    safety_triggered: bool = False
    integrity_violations: list[str] = Field(default_factory=list)


class ConversationState(TypedDict):
    """LangGraph 全局状态 - 跨所有节点共享"""

    # === 用户上下文 ===
    user_input: str
    user_profile: dict  # 地点/关系/时间/情形/遗嘱/家庭/财产
    session_id: str
    turn_count: int

    # === 当前活跃智能体 ===
    current_agent: str  # death-aftercare / legal-advisor / ...
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
    knowledge_query: Optional[dict]
    knowledge_results: list[dict]
    web_search_results: list[dict]

    # === 输出 ===
    draft_response: str
    final_response: str
    confidence_labels: list[dict]  # 置信度标注

    # === Reflexion ===
    reflexion_attempts: dict[str, int]  # {operation_id: attempt_count}
    reflexion_history: list[dict]

    # === 可观测性 ===
    trace_spans: list[dict]  # 本轮产生的 span
    metrics: dict  # 本轮指标
```

## 主 Graph 结构

```python
# orchestration/main_graph.py（伪代码）

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver  # 生产用


def build_main_graph():
    graph = StateGraph(ConversationState)

    # === 入口节点 ===
    graph.add_node("input_guard", input_guard_node)        # L2 input-guardrails
    graph.add_node("router", router_node)                  # 意图识别 + 选智能体
    graph.add_node("user_confirm_transfer", user_confirm_node)  # interrupt

    # === 6 个并列智能体节点（每个内部挂载 subgraph） ===
    graph.add_node("death_aftercare", death_aftercare_subgraph)
    graph.add_node("legal_advisor", legal_advisor_subgraph)
    graph.add_node("financial_analyst", financial_analyst_subgraph)
    graph.add_node("policy_researcher", policy_researcher_subgraph)
    graph.add_node("cross_border_specialist", cross_border_subgraph)
    graph.add_node("medical_guide", medical_guide_subgraph)

    # === 后置校验节点 ===
    graph.add_node("rule_check", rule_check_node)          # L0-L8 规则校验
    graph.add_node("integrity_check", integrity_check_node)  # 5 关事实复核
    graph.add_node("output_guard", output_guard_node)      # 输出前最终校验
    graph.add_node("respond", respond_node)                # 生成最终响应

    # === 入口边 ===
    graph.set_entry_point("input_guard")
    graph.add_edge("input_guard", "router")

    # === 路由边（条件） ===
    graph.add_conditional_edges(
        "router",
        route_to_agent,
        {
            "death_aftercare": "death_aftercare",
            "legal_advisor": "legal_advisor",
            "financial_analyst": "financial_analyst",
            "policy_researcher": "policy_researcher",
            "cross_border_specialist": "cross_border_specialist",
            "medical_guide": "medical_guide",
            "await_transfer_confirm": "user_confirm_transfer",
        },
    )

    # === 用户确认转介后 ===
    graph.add_conditional_edges(
        "user_confirm_transfer",
        after_user_confirm,
        {
            "proceed_transfer": "router",    # 用户同意，路由到目标智能体
            "decline_transfer": "respond",   # 用户拒绝，回应当前智能体
        },
    )

    # === 智能体执行后 ===
    for agent in ["death_aftercare", "legal_advisor", "financial_analyst",
                  "policy_researcher", "cross_border_specialist", "medical_guide"]:
        graph.add_edge(agent, "rule_check")

    # === 规则校验后 ===
    graph.add_conditional_edges(
        "rule_check",
        after_rule_check,
        {
            "safety_override": "respond",       # L0 触发，直接响应
            "needs_integrity_check": "integrity_check",
            "pass_through": "output_guard",
        },
    )

    graph.add_edge("integrity_check", "output_guard")
    graph.add_edge("output_guard", "respond")
    graph.add_edge("respond", END)

    # === 编译 ===
    checkpointer = PostgresSaver.from_conn_string(DB_URL)
    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["user_confirm_transfer"],  # 转介前暂停等用户确认
    )
```

## 路由节点（转介触发点）

```python
# orchestration/nodes/router.py（伪代码）

def route_to_agent(state: ConversationState) -> str:
    """根据用户输入和当前状态决定路由到哪个智能体"""

    # 1. 如果有待确认的转介且用户已确认 → 路由到目标智能体
    if state.get("pending_transfer") and state.get("transfer_confirmed") is True:
        return state["pending_transfer"].to_agent

    # 2. 如果有待确认的转介但未询问 → 进入用户确认节点
    if state.get("pending_transfer") and state.get("transfer_confirmed") is None:
        return "await_transfer_confirm"

    # 3. 安全优先：L0 触发时，若当前智能体不是 safety 专用分支，强制回到 death_aftercare
    if state.get("safety_override") and state["current_agent"] != "death_aftercare":
        return "death_aftercare"

    # 4. 正常路由：用 LLM 识别意图，匹配智能体 description
    intent = classify_intent(state["user_input"], state["user_profile"])
    return intent_to_agent(intent)


def classify_intent(user_input: str, user_profile: dict) -> str:
    """
    用 LLM 做意图分类。
    Prompt 中注入 6 个智能体的 description（来自 agents/*.md 的 frontmatter）。
    输出：death_aftercare / legal_advisor / financial_analyst /
          policy_researcher / cross_border_specialist / medical_guide
    """
    agent_descriptions = load_agent_descriptions()  # 从 agents/*.md 读
    prompt = f"""
    用户输入：{user_input}
    用户画像：{user_profile}

    可选智能体及其职责：
    {agent_descriptions}

    判定用户当前问题最适合哪个智能体处理。
    若用户在心理危机状态，强制路由到 death_aftercare。
    输出 JSON：{{"agent": "...", "reason": "...", "confidence": 0.0-1.0}}
    """
    return call_llm(prompt)
```

## 单个智能体 Subgraph（以 death_aftercare 为例）

```python
# orchestration/subgraphs/death_aftercare.py（伪代码）

def death_aftercare_subgraph(state: ConversationState) -> ConversationState:
    """death_aftercare 智能体的子图"""

    subgraph = StateGraph(ConversationState)

    # === 子图节点 ===
    subgraph.add_node("load_agent_md", load_agent_md_node)        # 加载 death-aftercare.md
    subgraph.add_node("load_rules", load_rules_node)              # 加载 rules/*.md
    subgraph.add_node("assess_emotion", assess_emotion_node)      # 调用 emotional 子智能体
    subgraph.add_node("track_progress", track_progress_node)      # 调用 tracker 子智能体
    subgraph.add_node("query_knowledge", query_knowledge_node)    # MCP query_knowledge
    subgraph.add_node("check_subagent_timing", check_timing_node) # 子智能体调用时机硬约束
    subgraph.add_node("draft_response", draft_response_node)      # 起草响应
    subgraph.add_node("detect_transfer_signals", detect_transfer_node)  # 检测转介信号

    # === 子图边 ===
    subgraph.set_entry_point("load_agent_md")
    subgraph.add_edge("load_agent_md", "load_rules")
    subgraph.add_edge("load_rules", "check_subagent_timing")

    # 子智能体调用时机硬约束：TEAM.md 定义的信号表
    subgraph.add_conditional_edges(
        "check_subagent_timing",
        decide_subagent_calls,
        {
            "call_emotional_only": "assess_emotion",
            "call_tracker_only": "track_progress",
            "call_both": "assess_emotion",  # 先 emotional，再 tracker
            "call_none": "query_knowledge",
        },
    )

    subgraph.add_edge("assess_emotion", "track_progress")  # 顺序：先情绪后跟进
    subgraph.add_edge("track_progress", "query_knowledge")
    subgraph.add_edge("query_knowledge", "draft_response")
    subgraph.add_edge("draft_response", "detect_transfer_signals")
    subgraph.add_edge("detect_transfer_signals", END)

    compiled = subgraph.compile()
    return compiled.invoke(state)


def decide_subagent_calls(state: ConversationState) -> str:
    """TEAM.md 定义的子智能体调用时机硬约束"""
    user_input = state["user_input"]
    signals = extract_signals(user_input)

    # 信号表（来自 TEAM.md "子智能体调用时机"章节）
    if signals.get("psychological_crisis") or signals.get("emotional_distress"):
        return "call_both"  # 危机/情绪信号 → 先 emotional 后 tracker
    if signals.get("progress_query") or state.get("turn_count", 0) > 3:
        return "call_tracker_only"
    if signals.get("first_turn"):
        return "call_both"  # 首轮：情绪 + 流程跟进
    return "call_none"


def assess_emotion_node(state: ConversationState) -> ConversationState:
    """调用 death-aftercare-emotional 子智能体，挂载 Reflexion"""
    from agents.Reflexion_Mechanism import execute_with_reflexion

    result = execute_with_reflexion(
        operation=invoke_subagent,
        initial_input={"subagent_name": "death-aftercare-emotional",
                       "user_input": state["user_input"]},
        operation_type="subagent",
    )

    subagent_result = SubagentResult(
        subagent_name="death-aftercare-emotional",
        execution_mode=result.get("execution_mode", "failed"),
        report=result.get("result", {}),
        confidence=result.get("confidence", 0.0),
        sources=result.get("sources", []),
    )
    state["subagent_results"].append(subagent_result)

    # 记录 trace span
    state["trace_spans"].append({
        "span_type": "subagent",
        "name": "subagent.death-aftercare-emotional",
        "attributes": {
            "execution_mode": subagent_result.execution_mode,
            "reflexion_attempts": result.get("attempts", 1),
            "fallback_used": result.get("fallback", False),
        },
    })

    # 危机检测 → 触发 safety_override
    if subagent_result.report.get("crisis_detected"):
        state["safety_override"] = True
        state["rule_check"] = RuleCheckResult(
            passed=False,
            risk_tier=RiskTier.R3,
            safety_triggered=True,
        )

    return state
```

## 规则校验节点（优先级链）

```python
# orchestration/nodes/rule_check.py（伪代码）

def rule_check_node(state: ConversationState) -> ConversationState:
    """L0-L8 规则优先级链校验"""

    # 加载所有规则文件（按优先级排序）
    rules = load_rules_in_priority_order()
    """
    优先级链（conflict-resolution.md）：
    L0 safety-protocol         (安全赢一切)
    L1 integrity-framework     (诚信赢温和)
    L2 input-guardrails        (输入防护)
    L3 compliance-framework    (合规)
    L4 risk-tier-framework     (风险分级)
    L5 transparency-framework  (透明)
    L6 accountability-framework(问责)
    L7 retrieval-guardrails    (检索)
    L8 tone-framework          (语气)
    """

    violations = []
    risk_tier = RiskTier.R0
    safety_triggered = False

    for rule in rules:
        result = rule.check(state["draft_response"], state)
        if not result.passed:
            violations.append({
                "rule": rule.name,
                "priority": rule.priority,
                "violation": result.violation,
                "suggestion": result.suggestion,
            })
            if rule.priority == 0:  # L0 safety
                safety_triggered = True
                risk_tier = RiskTier.R3
                break  # L0 触发立即终止其他校验
            if rule.priority == 4:  # L4 risk-tier
                risk_tier = RiskTier(result.risk_level)

    state["rule_check"] = RuleCheckResult(
        passed=len(violations) == 0,
        violations=violations,
        risk_tier=risk_tier,
        safety_triggered=safety_triggered,
        integrity_violations=[v for v in violations if v["priority"] == 1],
    )

    # 记录 trace span
    state["trace_spans"].append({
        "span_type": "rule",
        "name": "rule.check_all",
        "attributes": {
            "violations_count": len(violations),
            "risk_tier": risk_tier.value,
            "safety_triggered": safety_triggered,
        },
    })

    return state


def after_rule_check(state: ConversationState) -> str:
    """规则校验后的路由"""
    rc = state["rule_check"]
    if rc.safety_triggered:
        return "safety_override"  # 直接响应，跳过其他
    if rc.integrity_violations:
        return "needs_integrity_check"  # 5 关事实复核
    return "pass_through"
```

## 用户确认转介（Human-in-the-loop）

```python
# orchestration/nodes/user_confirm.py（伪代码）

def user_confirm_node(state: ConversationState) -> ConversationState:
    """
    转介前暂停，等用户确认。
    LangGraph 的 interrupt_before=["user_confirm_transfer"] 会在进入此节点前暂停。
    """
    transfer = state["pending_transfer"]

    # 生成转介话术
    prompt = f"""
    用户输入：{state['user_input']}
    转介摘要：{transfer.model_dump()}

    按以下要求生成转介话术（tone-framework）：
    1. 简短说明为什么建议转介
    2. 说明目标智能体能提供什么帮助
    3. 尊重用户自主权（不强制）
    4. 提供明确的"是/否"选择
    """
    transfer_message = call_llm(prompt)
    state["draft_response"] = transfer_message
    return state


def after_user_confirm(state: ConversationState) -> str:
    """用户确认后的路由"""
    if state["transfer_confirmed"] is True:
        # 记录转介历史
        state["transfer_history"].append(state["pending_transfer"])
        # 切换当前智能体
        state["current_agent"] = state["pending_transfer"].to_agent
        state["pending_transfer"] = None
        return "proceed_transfer"
    else:
        state["pending_transfer"] = None
        return "decline_transfer"
```

## Checkpointer（跨会话续接）

```python
# orchestration/checkpointer.py（伪代码）

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.base import CheckpointMetadata


def get_checkpointer():
    """生产环境用 Postgres，开发用 Memory"""
    return PostgresSaver.from_conn_string(DB_URL)


def resume_session(thread_id: str):
    """跨会话续接 - 与 Graphiti 时态记忆集成"""
    graph = build_main_graph()
    config = {"configurable": {"thread_id": thread_id}}

    # LangGraph 自动从 checkpoint 恢复状态
    # 包括：current_agent / user_profile / agent_history / transfer_history
    return graph


def save_user_progress(state: ConversationState):
    """
    每轮结束后，把用户进度写入 Graphiti 时态记忆。
    与 Temporal-Memory-Graphiti.md 集成。
    """
    graphiti = GraphitiClient()
    graphiti.add_event({
        "event_type": "UserProgressEvent",
        "session_id": state["session_id"],
        "user_profile": state["user_profile"],
        "current_agent": state["current_agent"],
        "completed_items": state.get("completed_items", []),
        "pending_items": state.get("pending_items", []),
        "timestamp": datetime.utcnow(),
    })
```

## 与现有设施的集成点

### 1. 与 MCP Server 集成

```python
# orchestration/nodes/mcp_tools.py（伪代码）

from mcp_server.client import MCPClient


def query_knowledge_node(state: ConversationState) -> ConversationState:
    """调用 MCP query_knowledge 工具"""
    mcp = MCPClient()

    # 若 LightRAG 试点已启用，用 hybrid 模式
    query_mode = "hybrid" if settings.LIGHTRAG_ENABLED else "vector"

    result = mcp.call_tool("query_knowledge", {
        "country": state["user_profile"]["country"],
        "region": state["user_profile"].get("region"),
        "topic": extract_topic(state["user_input"]),
        "query_mode": query_mode,  # LightRAG-Pilot.md 定义
    })

    state["knowledge_results"] = result["chunks"]
    state["trace_spans"].append({
        "span_type": "tool",
        "name": "tool.query_knowledge",
        "attributes": {
            "tool_name": "query_knowledge",
            "query_mode": query_mode,
            "results_count": len(result["chunks"]),
            "sources": [c["source"] for c in result["chunks"]],
        },
    })
    return state
```

### 2. 与 Reflexion 机制集成

子智能体调用、工具调用、转介调用都挂载 Reflexion：

```python
# orchestration/reflexion_integration.py（伪代码）

from agents.Reflexion_Mechanism import execute_with_reflexion


def invoke_subagent_with_reflexion(subagent_name, user_input):
    return execute_with_reflexion(
        operation=invoke_subagent,
        initial_input={"subagent_name": subagent_name, "user_input": user_input},
        operation_type="subagent",
    )


def call_tool_with_reflexion(tool_name, args):
    return execute_with_reflexion(
        operation=mcp.call_tool,
        initial_input={"tool_name": tool_name, "args": args},
        operation_type="tool",
    )


def init_transfer_with_reflexion(transfer_summary):
    return execute_with_reflexion(
        operation=init_transfer,
        initial_input={"transfer": transfer_summary},
        operation_type="transfer",
    )
```

### 3. 与可观测性集成

```python
# orchestration/observability.py（伪代码）

from opentelemetry import trace
from observability.otel_setup import tracer


def instrument_graph(graph):
    """给 graph 的每个节点加 OTel span"""

    @tracer.trace("langgraph.node")
    def wrapped_node(node_name, original_fn):
        def wrapped(state):
            with tracer.start_as_current_span(f"node.{node_name}") as span:
                span.set_attribute("node.name", node_name)
                span.set_attribute("state.current_agent", state.get("current_agent"))
                span.set_attribute("state.turn_count", state.get("turn_count", 0))
                result = original_fn(state)
                # 把 state 中的 trace_spans flush 到 OTel
                for s in result.get("trace_spans", []):
                    emit_span(s)
                return result
        return wrapped

    return apply_wrapper(graph, wrapped_node)


def stream_to_langfuse(graph, stream_mode="values"):
    """
    LangGraph stream → Langfuse trace。
    与 OTel-Integration-Guide.md 集成。
    """
    for event in graph.stream(stream_mode=stream_mode):
        langfuse.trace(event)
        yield event
```

### 4. 与 SelfCheckGPT 集成

```python
# orchestration/nodes/integrity_check.py（伪代码）

def integrity_check_node(state: ConversationState) -> ConversationState:
    """5 关事实复核 + SelfCheckGPT 数字类校验"""
    from tests.automated.SelfCheckGPT import check_numeric_consistency

    # 1. check_integrity MCP 工具（5 关）
    mcp = MCPClient()
    integrity_result = mcp.call_tool("check_integrity", {
        "output_text": state["draft_response"],
        "knowledge_results": state["knowledge_results"],
    })

    # 2. SelfCheckGPT 数字类一致性校验
    numeric_check = check_numeric_consistency(
        response=state["draft_response"],
        sampled_responses=sample_multiple(state["user_input"], n=5),
    )

    # 3. 若发现不一致，触发 Reflexion 重写
    if not integrity_result["passed"] or numeric_check["consistency"] < 0.5:
        state["draft_response"] = rewrite_with_reflexion(
            state["draft_response"],
            failure_reason="integrity_or_numeric_inconsistency",
        )

    return state
```

## 平台无关性声明

本文件以 LangGraph 为参考实现，但映射规则适用于所有平台：

### TRAE 平台实现

```
- 6 个 agent.md 文件 → 6 个 TRAE Agent
- subgraph → TRAE subagent（通过 description 自动匹配）
- conditional edge → TRAE 推荐机制
- interrupt → TRAE 用户输入节点
- checkpointer → TRAE 会话历史
```

### Coze 平台实现

```
- 6 个 node → 6 个 Coze Bot
- subgraph → Coze 插件调用
- conditional edge → Coze 意图路由
- interrupt → Coze 用户确认卡片
- checkpointer → Coze 会话变量
```

### Dify 平台实现

```
- 6 个 node → 6 个 Dify Agent 节点
- subgraph → Dify 工作流子流程
- conditional edge → Dify 条件分支
- interrupt → Dify 人工审批节点
- checkpointer → Dify 会话变量
```

## 部署架构

```
                    用户
                      ↓
              ┌───────────────┐
              │  API Gateway  │  (FastAPI)
              └───────┬───────┘
                      ↓
              ┌───────────────┐
              │  LangGraph    │
              │  Runtime      │  ← main_graph.compile()
              └───────┬───────┘
                      ↓
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
   ┌─────────┐  ┌──────────┐  ┌──────────┐
   │ MCP     │  │ Graphiti │  │ Langfuse │
   │ Server  │  │ (Neo4j)  │  │ (OTel)   │
   └─────────┘  └──────────┘  └──────────┘
        ↓
   ┌─────────┐
   │ LightRAG│
   │ (向量库) │
   └─────────┘
```

## 评估指标（与 Metrics.md 对齐）

| 指标 | 目标 | 说明 |
|------|------|------|
| 路由准确率 | ≥ 0.95 | router_node 选对智能体的比例 |
| 转介准确率 | 1.0 | 转介到正确目标智能体 |
| 规则校验通过率 | ≥ 0.95 | rule_check_node 无 violation |
| 子智能体调用时机准确率 | ≥ 0.90 | 符合 TEAM.md 硬约束 |
| 转介摘要完整度 | 1.0 | 7 字段齐全 |
| checkpoint 恢复成功率 | 1.0 | 跨会话续接无丢失 |
| 端到端延迟 P95 | ≤ 8s | 单轮响应 |
| Reflexion 触发率 | ≤ 0.20 | 失败重试占比 |

## 测试集成

### 与 tests/automated/ 集成

```python
# orchestration/test_runner.py（伪代码）

def run_golden_cases():
    """跑 tests/automated/cases/ 下的所有 YAML"""
    graph = build_main_graph()

    for case_file in glob("tests/automated/cases/*.yaml"):
        case = load_yaml(case_file)

        # 用 case 的 user_input 跑 graph
        result = graph.invoke({
            "user_input": case["user_input"],
            "user_profile": case.get("context", {}),
            "session_id": f"test-{case['case_id']}",
        }, config={"configurable": {"thread_id": f"test-{case['case_id']}"}})

        # 三层判定
        evaluate_response(result["final_response"], case)

        # 工具调用序列校验（Expected-Tool-Calls.md）
        evaluate_tool_calls(result["trace_spans"], case.get("expected_tool_calls", []))
```

## 版本

- v1.0 初始 LangGraph 编排底座方案（StateGraph + 转介映射 + 子智能体 subgraph + 规则校验 + checkpoint + 平台无关映射）
```
