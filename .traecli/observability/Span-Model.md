# Span 模型定义

> 本文件定义多智能体系统的 6 类 span，用于结构化 trace。借鉴 OpenTelemetry span 模型，针对多智能体场景扩展。

## 设计理念

一次完整的用户交互被建模为一棵 span tree：
- root span = 一次用户请求
- child span = 智能体处理、子智能体调用、转介、规则触发、工具调用

每个 span 记录：起始时间、结束时间、属性（attributes）、状态（status）、事件（events）。

## 6 类 Span

### 1. Root Span（根 span）

**含义**：一次完整的用户请求，从用户输入到最终响应。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": null,
  "name": "user_request",
  "span_type": "root",
  "start_time": "2026-07-12T10:00:00Z",
  "end_time": "2026-07-12T10:02:30Z",
  "attributes": {
    "user_id_hash": "sha256_hash",
    "session_id": "session_uuid",
    "platform": "trae",
    "user_input_length": 156,
    "user_input_pii_redacted": true,
    "risk_tier_initial": "R1",
    "risk_tier_final": "R2",
    "rules_triggered": ["safety-protocol", "integrity-framework", "compliance-framework"],
    "agents_involved": ["death-aftercare", "financial-analyst"],
    "transfers_count": 1,
    "subagent_calls_count": 2,
    "final_response_length": 892,
    "outcome": "completed|transferred|escalated|blocked|error"
  },
  "status": "OK|ERROR|PARTIAL",
  "events": [
    {"name": "risk_tier_upgrade", "timestamp": "...", "from": "R1", "to": "R2", "reason": "..."}
  ]
}
```

**关键属性说明**：
- `user_id_hash`：用户 ID 的哈希，不存原始 ID（PII 脱敏）
- `user_input_pii_redacted`：输入是否已脱敏
- `risk_tier_initial` → `risk_tier_final`：风险等级变化（如从 R1 升级到 R2）
- `rules_triggered`：本次触发的规则文件列表
- `outcome`：最终结果类型

### 2. Agent Span（智能体 span）

**含义**：一个并列智能体对用户请求的处理。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "root_span_id",
  "name": "agent.death-aftercare",
  "span_type": "agent",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "agent_name": "death-aftercare",
    "agent_role": "flow_guide",
    "entry_mode": "direct|transfer",
    "transferred_from": "medical-guide|null",
    "transfer_summary_received": true,
    "transfer_summary_fields_complete": 7,
    "transfer_summary_fields_missing": ["已完成事项"],
    "context_loaded": ["rules/safety-protocol.md", "rules/integrity-framework.md", "knowledge/regions/CN/overview.md"],
    "rules_checked": ["L0", "L1", "L2", "L3", "L4", "L5", "L8"],
    "rules_triggered": ["L1.integrity.质疑", "L4.risk-tier.R2"],
    "integrity_self_check_passed": true,
    "integrity_self_check_failures": [],
    "confidence_labels_output": ["高", "中", "中", "未知"],
    "sources_cited": ["https://...", "https://..."],
    "subagent_calls": ["death-aftercare-emotional", "death-aftercare-tracker"],
    "transfers_initiated": [{"to": "financial-analyst", "user_confirmed": true}],
    "response_length": 892,
    "ai_identity_disclosed": true,
    "disclaimer_included": true
  },
  "status": "OK|ERROR|PARTIAL",
  "events": [
    {"name": "rule_triggered", "timestamp": "...", "rule": "L1.integrity.质疑", "reason": "用户表述时间矛盾"},
    {"name": "subagent_called", "timestamp": "...", "subagent": "death-aftercare-emotional", "purpose": "评估情绪等级"},
    {"name": "transfer_initiated", "timestamp": "...", "to": "financial-analyst", "reason": "复杂财务"}
  ]
}
```

**关键属性说明**：
- `entry_mode`：direct（用户直接来访）或 transfer（转介过来）
- `transfer_summary_fields_complete/missing`：转介摘要完整性（可量化"转介摘要完整率"）
- `rules_triggered`：精确到规则文件+章节
- `integrity_self_check_*`：integrity-framework 第八章 5 关自检的结果
- `confidence_labels_output`：输出中标注的置信度列表
- `sources_cited`：引用的来源 URL（可校验来源透传）
- `ai_identity_disclosed`：是否告知 AI 身份（transparency-framework）
- `disclaimer_included`：是否含免责声明

### 3. Subagent Span（子智能体 span）

**含义**：父智能体调用私有子智能体的执行。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id",
  "name": "subagent.death-aftercare-emotional",
  "span_type": "subagent",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "subagent_name": "death-aftercare-emotional",
    "parent_agent": "death-aftercare",
    "invoke_purpose": "评估情绪等级|生成情绪支持话术|判断是否需转safety-protocol",
    "invoke_signal": "R3心理危机信号|用户表达强烈情绪",
    "input_summary": "用户表达撑不下去想跟着走",
    "input_pii_redacted": true,
    "tools_used": ["Read"],
    "result_summary": "评估为R3心理危机，建议立即触发safety-protocol",
    "result_schema_valid": true,
    "execution_mode": "success|fallback|failed",
    "fallback_reason": "平台不支持subagent|null",
    "latency_ms": 1200
  },
  "status": "OK|ERROR|FALLBACK",
  "events": []
}
```

**关键属性说明**：
- `invoke_signal`：触发的信号（对应 TEAM.md 子智能体调用时机表）
- `execution_mode`：success/fallback/failed（对应 TEAM.md 调用失败处理）
- `result_schema_valid`：返回报告是否符合结构化 schema

### 4. Transfer Span（转介 span）

**含义**：智能体之间的转介事件。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id",
  "name": "transfer.death-aftercare->financial-analyst",
  "span_type": "transfer",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "from_agent": "death-aftercare",
    "to_agent": "financial-analyst",
    "transfer_reason": "复杂财务（多资产+税务）",
    "transfer_signal": "用户涉及多类资产",
    "transfer_summary": {
      "转介自": "death-aftercare",
      "转介原因": "复杂财务",
      "用户情况": "北京/独生女/前天去世",
      "已确认": "无遗嘱/单独继承/多类资产",
      "已完成事项": "尚未办理任何手续",
      "当前问题": "资产清点与税务",
      "上下文传递": "已加载CN/overview.md"
    },
    "transfer_summary_fields_count": 7,
    "transfer_summary_fields_complete": 7,
    "transfer_summary_fields_missing": [],
    "user_confirmed": true,
    "user_decision_time_ms": 8500,
    "cross_team": false
  },
  "status": "OK|DECLINED|ERROR",
  "events": [
    {"name": "user_confirmed", "timestamp": "..."}
  ]
}
```

**关键属性说明**：
- `transfer_summary_fields_complete/missing`：转介摘要 7 字段完整性
- `user_confirmed`：用户是否确认转介（转介是建议非强制）
- `cross_team`：是否跨团队转介（身后事 ↔ 医疗导航）

### 5. Rule Span（规则触发 span）

**含义**：规则优先级链的裁决过程。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id",
  "name": "rule.L1.integrity.质疑",
  "span_type": "rule",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "rule_file": "integrity-framework.md",
    "rule_priority_level": "L1",
    "rule_chapter": "四、主动质疑用户矛盾准则",
    "triggered_clause": "1.前后表述矛盾",
    "trigger_reason": "用户说前天去世又上周过户，时间对不上",
    "conflict_detected": true,
    "conflicting_rules": [],
    "resolution": "L1优先于L8语气，执行质疑",
    "deferred": false,
    "deferred_until": null,
    "deferred_reason": null,
    "action_taken": "礼貌指出时间矛盾",
    "user_response": "承认记错时间"
  },
  "status": "OK",
  "events": [
    {"name": "conflict_resolution", "timestamp": "...", "rule_a": "L1.integrity", "rule_b": "L8.tone", "winner": "L1"}
  ]
}
```

**关键属性说明**：
- `conflict_detected`：是否与其他规则冲突
- `resolution`：冲突裁决结果（对应 conflict-resolution.md）
- `deferred`：是否延后执行（integrity-framework 七·补：安全优先时的延后质疑）
- `action_taken`：实际执行的动作

### 6. Tool Span（工具调用 span）

**含义**：智能体调用外部工具（WebSearch/WebFetch/Read/Write 等）。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id|subagent_span_id",
  "name": "tool.web_search",
  "span_type": "tool",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "tool_name": "web_search|web_fetch|read_file|write_file|invoke_subagent|recommend_agent",
    "tool_category": "search|fetch|storage|agent_call",
    "args_summary": "query: 加州继承法 海牙认证",
    "args_pii_redacted": true,
    "result_summary": "返回10条结果，3条官方源",
    "result_length": 2400,
    "official_sources_count": 3,
    "sources_trust_level": ["high", "high", "high", "medium", "low"],
    "latency_ms": 1800,
    "cost_usd": 0.002,
    "success": true,
    "error_type": null
  },
  "status": "OK|ERROR|TIMEOUT",
  "events": []
}
```

**关键属性说明**：
- `sources_trust_level`：来源信任分级（对应 retrieval-guardrails）
- `official_sources_count`：官方源数量（可量化检索质量）
- `cost_usd`：调用成本（成本可观测）

## Span 之间的关系

```
root_span (user_request)
├── agent_span (death-aftercare)
│   ├── rule_span (L0.safety 检查)
│   ├── rule_span (L1.integrity 质疑)
│   │   └── event: conflict_resolution (L1 vs L8, L1 赢)
│   ├── rule_span (L4.risk-tier R2 升级)
│   ├── subagent_span (death-aftercare-emotional)
│   │   └── tool_span (read_file: rules/safety-protocol.md)
│   ├── tool_span (read_file: knowledge/regions/CN/overview.md)
│   ├── tool_span (web_search: 北京户籍注销)
│   └── transfer_span (death-aftercare → financial-analyst)
│       └── event: user_confirmed
├── agent_span (financial-analyst)
│   ├── subagent_span (financial-analyst-assets)
│   │   └── tool_span (read_file: ...)
│   └── subagent_span (financial-analyst-taxes)
│       ├── tool_span (web_search: ...)
│       └── tool_span (web_search: ...)
└── event: risk_tier_upgrade (R1 → R2)
```

## JSONL Trace 文件格式

trace 持久化为 JSONL（每行一个 span），存入 `knowledge/_traces/`：

```
knowledge/_traces/
├── 2026-07-12/
│   ├── trace_{uuid}.jsonl    # 一个 trace 文件，含该 trace 的所有 span
│   ├── trace_{uuid}.jsonl
│   └── ...
└── 2026-07-13/
    └── ...
```

每个 JSONL 文件第一行是 root span，后续行是 child span，通过 parent_span_id 串联。

## 与 accountability-framework 的联动

事故记录（`knowledge/_incidents/`）升级为引用 trace：

```json
{
  "incident_id": "uuid",
  "trace_id": "uuid",
  "timestamp": "...",
  "severity": "high|medium|low",
  "description": "智能体编造了派出所电话号码",
  "root_cause_span_id": "span_uuid",
  "root_cause_rule_violation": "integrity-framework.md 一、1.禁止编造电话号码",
  "trace_file": "knowledge/_traces/2026-07-12/trace_{uuid}.jsonl",
  "corrective_action": "已更新知识库，标注未知",
  "user_notified": true,
  "user_appeal": null
}
```

事故调查时可通过 `trace_id` 找到完整 trace，逐 span 复盘。

## v1.1 新增 Span 类型（v4.2 支撑设施）

以下 5 类 span 在 v1.1 新增，对应 P1/P2 支撑设施：

### 7. Debate Span（辩论 span）

**含义**：多智能体辩论会话（[Debate-Voting.md](../agents/Debate-Voting.md)）。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id",
  "name": "debate.session",
  "span_type": "debate",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "debate_id": "uuid",
    "topic": "跨境继承适用哪国法律",
    "participants": ["cross-border-specialist", "legal-advisor"],
    "rounds_count": 3,
    "voting_strategy": "weighted",
    "votes": {"cross-border-specialist": 2, "legal-advisor": 1},
    "winner": "cross-border-specialist",
    "consensus_reached": true,
    "arbitration_needed": false,
    "final_resolution": "适用不动产所在地法",
    "final_confidence": 0.8
  },
  "status": "OK|PARTIAL|ERROR",
  "events": [
    {"name": "round_completed", "timestamp": "...", "round": 1, "type": "opening"},
    {"name": "vote_cast", "timestamp": "...", "voter": "financial-analyst", "vote_for": "cross-border-specialist"}
  ]
}
```

### 8. Memory Span（记忆 span）

**含义**：分层记忆的查询/更新操作（[Memory-Store.md](../agents/Memory-Store.md)）。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id",
  "name": "memory.recall",
  "span_type": "memory",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "action": "recall|update_profile|update_progress|detect_contradiction",
    "memory_layer": "working|episodic|semantic|procedural",
    "user_id_hash": "sha256_hash",
    "results_count": 5,
    "contradictions_detected": 0,
    "latency_ms": 120,
    "graphiti_synced": true
  },
  "status": "OK|ERROR",
  "events": []
}
```

### 9. A2A Span（跨厂商调用 span）

**含义**：通过 A2A 协议调用外部智能体（[A2A-Protocol.md](../a2a/A2A-Protocol.md)）。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id",
  "name": "a2a.call_external",
  "span_type": "a2a",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "from_agent_id": "legacy-legal-advisor",
    "to_agent_id": "external-lawyer-agent",
    "capability_id": "inheritance-law-consultation",
    "task_state": "completed|failed|rejected",
    "user_consent_obtained": true,
    "pii_redacted": true,
    "integrity_report_received": true,
    "integrity_verified": true,
    "latency_ms": 8500,
    "cross_vendor": true
  },
  "status": "OK|ERROR|REJECTED",
  "events": [
    {"name": "user_consent_obtained", "timestamp": "..."},
    {"name": "task_completed", "timestamp": "..."}
  ]
}
```

### 10. Reflexion Span（反思重试 span）

**含义**：子智能体/工具/转介调用失败后的反思-调整-重试（[Reflexion-Mechanism.md](../agents/Reflexion-Mechanism.md)）。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "agent_span_id|subagent_span_id",
  "name": "reflexion.retry",
  "span_type": "reflexion",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "operation_type": "subagent|tool|transfer",
    "operation_name": "death-aftercare-emotional",
    "failure_reason": "timeout",
    "attempts_made": 2,
    "max_retries": 3,
    "success": true,
    "fallback_used": false,
    "adjustments_applied": ["简化任务描述，减少上下文"],
    "strategy_used": "timeout",
    "graphiti_learned": true
  },
  "status": "OK|FALLBACK|ERROR",
  "events": [
    {"name": "attempt_failed", "timestamp": "...", "attempt": 1, "reason": "timeout"},
    {"name": "reflection_generated", "timestamp": "...", "reflection": "简化输入重试"},
    {"name": "attempt_succeeded", "timestamp": "...", "attempt": 2}
  ]
}
```

### 11. LLM Judge Span（LLM 评审 span）

**含义**：LLM-as-Judge 的评审调用（[LLM-as-Judge.md](../tests/automated/LLM-as-Judge.md)）。

```json
{
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "root_span_id|agent_span_id",
  "name": "tool.llm_judge",
  "span_type": "llm_judge",
  "start_time": "...",
  "end_time": "...",
  "attributes": {
    "case_id": "01",
    "judge_models": ["gpt-4o", "claude-3-5-sonnet", "glm-4.6"],
    "consensus": "通过|失败|需人工复核",
    "agreement_rate": 1.0,
    "pass_count": 3,
    "fail_count": 0,
    "latency_ms": 3200,
    "cost_usd": 0.05,
    "layer_reached": "llm"
  },
  "status": "OK",
  "events": []
}
```

## 更新后的 Span 关系树

```
root_span (user_request)
├── agent_span (death-aftercare)
│   ├── rule_span (L0.safety 检查)
│   ├── rule_span (L1.integrity 质疑)
│   ├── memory_span (recall episodic)          ← v1.1 新增
│   ├── subagent_span (death-aftercare-emotional)
│   │   ├── reflexion_span (retry)             ← v1.1 新增
│   │   └── tool_span (read_file)
│   ├── tool_span (query_knowledge)
│   │   └── reflexion_span (retry)             ← v1.1 新增（若查询失败）
│   ├── debate_span (cross-border vs legal)    ← v1.1 新增
│   │   └── event: vote_cast
│   ├── a2a_span (call external lawyer agent)  ← v1.1 新增
│   ├── transfer_span (death-aftercare → financial-analyst)
│   └── llm_judge_span (case 01 评审)          ← v1.1 新增
└── event: risk_tier_upgrade (R1 → R2)
```

## 版本
- v1.1 新增 5 类 span（debate/memory/a2a/reflexion/llm_judge），共 11 类 span，对应 v4.2 支撑设施
- v1.0 初始 span 模型（6 类 span + JSONL 格式 + 事故联动）
