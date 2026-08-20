# MCP Server 封装方案

> 本文件定义如何把平台的 rules 校验、knowledge 查询、转介机制封装为标准 MCP（Model Context Protocol）工具。借鉴 Anthropic MCP（2024.11 开源）、FastMCP、Neo4j GraphRAG MCP server。

## 为什么做 MCP 封装

### 当前痛点

13 个平台适配时，同样的能力（规则校验、知识库查询、转介）要在每个平台用不同的工具格式重写：
- TRAE：frontmatter `tools:` 字段
- OpenAI：function calling JSON Schema
- Claude：tool_use 格式
- 百炼：插件 OpenAPI
- 元宝：插件配置

这导致：
1. **重复开发**：同一能力写 13 遍
2. **不一致风险**：某平台漏了某个规则校验
3. **维护成本**：rules 变更时要改 13 处

### MCP 解决方案

把核心能力封装为 MCP server，所有支持 MCP 的平台（Claude/OpenAI Agents SDK/TRAE/自建）直接调用，一次实现多处复用。

```
┌──────────────────────────────────────────┐
│  各平台（Claude/OpenAI/TRAE/...）         │
│  统一用 MCP 协议调用                      │
└────────────────┬─────────────────────────┘
                 │ MCP Protocol
┌────────────────▼─────────────────────────┐
│  平台 MCP Server（15 工具）               │
│  ├── check_rules（规则校验）              │
│  ├── query_knowledge（知识库查询）        │
│  ├── check_integrity（5 关自检）          │
│  ├── web_search（网络搜索）               │
│  ├── web_search_official（官方源搜索）    │
│  ├── execute_code（沙箱代码执行）         │
│  ├── read_file（读取文件）                │
│  ├── write_file（写入文件）               │
│  ├── invoke_subagent（调用子智能体）      │
│  ├── query_memory（分层记忆查询）         │
│  ├── initiate_debate（发起辩论）          │
│  ├── call_external_agent（A2A 调用）      │
│  ├── execute_reflexion（反思重试）        │
│  ├── init_transfer（转介初始化）          │
│  └── report_incident（事故上报）          │
└────────────────┬─────────────────────────┘
                 │ Read/Write
┌────────────────▼─────────────────────────┐
│  文件系统                                  │
│  ├── rules/                               │
│  ├── knowledge/regions/                   │
│  └── knowledge/_traces/                   │
└──────────────────────────────────────────┘
```

## MCP Server 工具定义

### 1. check_rules（规则校验）

**功能**：输入智能体的待输出内容，返回规则校验结果。

```json
{
  "name": "check_rules",
  "description": "校验智能体输出是否符合规则。输入待校验文本和智能体名，返回违反的规则列表。必须在输出给用户前调用。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "agent_name": {"type": "string", "description": "智能体名，如 death-aftercare"},
      "output_text": {"type": "string", "description": "待校验的输出内容"},
      "context": {
        "type": "object",
        "properties": {
          "user_input": {"type": "string"},
          "risk_tier": {"type": "string", "enum": ["R1", "R2", "R3"]},
          "rules_to_check": {"type": "array", "items": {"type": "string"}}
        }
      }
    },
    "required": ["agent_name", "output_text"]
  },
  "output": {
    "passed": "boolean",
    "violations": [
      {
        "rule_file": "integrity-framework.md",
        "chapter": "一、1.禁止编造",
        "severity": "L1",
        "reason": "输出中出现了无来源的具体数字",
        "suggestion": "删除数字或标注来源"
      }
    ],
    "warnings": [
      {
        "rule_file": "transparency-framework.md",
        "chapter": "AI身份告知",
        "severity": "L5",
        "reason": "本次输出未告知AI身份",
        "suggestion": "首次交互需告知"
      }
    ]
  }
}
```

**实现逻辑**：
1. 读取 `rules/` 目录
2. 按智能体 context 加载相关规则
3. 用正则 + 关键词 + LLM 三层校验
4. 返回 violations 和 warnings

### 2. query_knowledge（知识库查询）

**功能**：查询地域知识库，返回结构化结果 + 信任分级。v1.1 起支持 LightRAG 知识图谱模式（[LightRAG-Pilot.md](../knowledge/LightRAG-Pilot.md)）和跨域本体过滤（[Cross-Domain-Ontology.md](../knowledge/Cross-Domain-Ontology.md)）。

```json
{
  "name": "query_knowledge",
  "description": "查询地域知识库。输入国家、地区、查询主题，返回当地政策信息。若知识库不存在，返回'需触发policy-researcher搜索'。支持 LightRAG 知识图谱检索模式（local/global/hybrid）和跨域本体实体/关系类型过滤。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "country": {"type": "string", "description": "国家代码，如 CN/US/JP"},
      "region": {"type": "string", "description": "地区，如 beijing/california/tokyo"},
      "topic": {"type": "string", "description": "查询主题，如 death_certificate/estate_inheritance"},
      "fallback_to_search": {"type": "boolean", "description": "知识库不存在时是否建议触发搜索"},
      "query_mode": {"type": "string", "enum": ["vector", "local", "global", "hybrid"], "default": "vector", "description": "检索模式：vector=传统向量检索（默认）；local/global/hybrid=LightRAG 知识图谱模式（见 LightRAG-Pilot.md）"},
      "entity_types": {"type": "array", "items": {"type": "string"}, "description": "按跨域本体实体类型过滤，如 ['DeathCertificate','Organization']（见 Cross-Domain-Ontology.md）"},
      "relation_types": {"type": "array", "items": {"type": "string"}, "description": "按跨域本体关系类型过滤，如 ['requires','issued_by']"}
    },
    "required": ["country", "topic"]
  },
  "output": {
    "found": "boolean",
    "data": {
      "content": "知识库内容片段",
      "last_updated": "2026-07-01",
      "sources": ["url1", "url2"],
      "trust_level": "high|medium|low",
      "freshness_status": "fresh|stale|outdated"
    },
    "graph_entities": [{"type": "DeathCertificate", "name": "...", "properties": {}}],
    "graph_relations": [{"source": "...", "type": "requires", "target": "...", "source_text": "原文片段"}],
    "needs_research": "boolean",
    "research_suggestion": "建议触发policy-researcher搜索加州政策"
  }
}
```

### 3. check_integrity（5 关自检 + SelfCheckGPT 数字类校验）

**功能**：integrity-framework 第八章的 5 关输出自检。v1.1 起集成 SelfCheckGPT 数字类幻觉检测（[SelfCheckGPT.md](../tests/automated/SelfCheckGPT.md)）。

```json
{
  "name": "check_integrity",
  "description": "输出前5关事实复核 + SelfCheckGPT 数字类一致性校验。校验来源、幻觉、时效、单源、越界，并对数字类 claim（电话/天数/金额/百分比/条文号/步骤数）做多次采样一致性检测。必须在输出具体事实性信息前调用。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "output_text": {"type": "string"},
      "claims_to_verify": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "claim": {"type": "string", "description": "待验证的具体陈述"},
            "source": {"type": "string", "description": "来源URL或文件"},
            "claim_type": {"type": "string", "enum": ["fact", "number", "legal_citation", "procedure", "phone_number"]}
          }
        }
      },
      "selfcheck_enabled": {"type": "boolean", "default": true, "description": "是否启用 SelfCheckGPT 数字类一致性校验（见 SelfCheckGPT.md）"},
      "selfcheck_sample_count": {"type": "integer", "default": 5, "description": "SelfCheckGPT 采样次数（3-5）"}
    },
    "required": ["output_text", "claims_to_verify"]
  },
  "output": {
    "passed": "boolean",
    "check_results": {
      "source_check": {"passed": true, "issues": []},
      "hallucination_check": {"passed": false, "issues": ["电话号码无来源"]},
      "freshness_check": {"passed": true, "issues": []},
      "single_source_check": {"passed": true, "issues": []},
      "boundary_check": {"passed": true, "issues": []}
    },
    "selfcheck_result": {
      "numeric_claims_found": 3,
      "consistency_scores": [{"claim": "30天", "consistency": 0.9, "label": "高"}, {"claim": "12345", "consistency": 0.2, "label": "未知"}],
      "overall_consistency": 0.65,
      "low_consistency_claims": ["12345"]
    },
    "confidence_labels": [
      {"claim": "北京户籍注销时限30天", "confidence": "高", "source": "knowledge/regions/CN/overview.md"},
      {"claim": "派出所电话12345", "confidence": "未知", "source": null}
    ]
  }
}
```

**实现逻辑**：
1. 执行 5 关校验（来源/幻觉/时效/单源/越界）
2. 若 `selfcheck_enabled`：提取数字类 claim（6 种正则：phone/days/money/percent/article/step_count）
3. 多次采样（temp=0.3/0.5/0.7/0.4/0.6）生成响应，计算数字类 claim 的一致性
4. 一致性 < 0.5 的 claim 标记为"未知"，触发重写或标注

### 4. web_search（网络搜索）

**功能**：封装 WebSearch 能力，供智能体在 MCP 统一接口下搜索实时信息。

```json
{
  "name": "web_search",
  "description": "搜索网络获取实时信息。用于知识库未覆盖或需核实的政策、电话、流程等。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "搜索查询"},
      "num_results": {"type": "integer", "default": 5, "description": "返回结果数量"}
    },
    "required": ["query"]
  },
  "output": {
    "results": [
      {"title": "...", "url": "...", "snippet": "..."}
    ]
  }
}
```

### 5. read_file（读取文件）

**功能**：读取工作区文件，供智能体加载规则、知识库、配置等。

```json
{
  "name": "read_file",
  "description": "读取工作区内指定路径的文件内容。用于加载 rules/、knowledge/ 等文件。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "file_path": {"type": "string", "description": "文件路径，如 rules/safety-protocol.md"},
      "encoding": {"type": "string", "default": "utf-8"}
    },
    "required": ["file_path"]
  },
  "output": {
    "content": "文件内容",
    "exists": "boolean",
    "size": "integer"
  }
}
```

### 6. write_file（写入文件）

**功能**：写入工作区文件，用于知识库更新、incident 记录、trace 存储等。受权限隔离约束，仅允许写入 knowledge/_traces/、knowledge/_incidents/ 等指定目录。

```json
{
  "name": "write_file",
  "description": "将内容写入工作区指定路径。用于知识库更新、incident 记录等。受权限隔离约束。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "file_path": {"type": "string", "description": "目标文件路径"},
      "content": {"type": "string", "description": "写入内容"},
      "mode": {"type": "string", "enum": ["write", "append"], "default": "write"}
    },
    "required": ["file_path", "content"]
  },
  "output": {
    "written": "boolean",
    "bytes": "integer"
  }
}
```

### 7. invoke_subagent（调用子智能体）

**功能**：调用子智能体在独立上下文执行深度任务，结果以结构化报告返回父智能体。子智能体不直接面对用户。

```json
{
  "name": "invoke_subagent",
  "description": "调用子智能体在独立上下文执行深度任务。子智能体不直接面对用户，结果以结构化报告返回父智能体。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "subagent_name": {"type": "string", "description": "子智能体名，如 death-aftercare-tracker"},
      "task": {"type": "string", "description": "任务描述"},
      "context": {"type": "object", "description": "传递给子智能体的上下文"},
      "timeout": {"type": "integer", "default": 60, "description": "超时秒数"}
    },
    "required": ["subagent_name", "task"]
  },
  "output": {
    "success": "boolean",
    "result": "dict|null",
    "report": "string",
    "error": "string|null"
  }
}
```

### 8. query_memory（分层记忆查询）

**功能**：查询分层记忆系统（[Memory-Store.md](../agents/Memory-Store.md)）。支持 Working/Episodic/Semantic/Procedural 四层记忆的查询和更新。

```json
{
  "name": "query_memory",
  "description": "查询或更新分层记忆。支持工作记忆（最近对话）、情景记忆（历史片段）、语义记忆（用户画像/事实）、程序记忆（流程进度）。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "enum": ["recall", "update_profile", "update_progress", "detect_contradiction"]},
      "user_id": {"type": "string", "description": "用户 ID（哈希）"},
      "memory_layer": {"type": "string", "enum": ["working", "episodic", "semantic", "procedural"]},
      "query": {"type": "string", "description": "recall 时的查询文本"},
      "updates": {"type": "object", "description": "update_profile/update_progress 时的更新内容"}
    },
    "required": ["action", "user_id"]
  },
  "output": {
    "action": "recall|update_profile|update_progress|detect_contradiction",
    "results": "list[dict]",
    "contradictions_detected": "list[dict]",
    "user_profile": "dict",
    "current_progress": "dict"
  }
}
```

### 9. initiate_debate（发起辩论）

**功能**：当多智能体意见冲突时发起辩论（[Debate-Voting.md](../agents/Debate-Voting.md)）。

```json
{
  "name": "initiate_debate",
  "description": "当多个智能体对同一问题给出冲突回答时，发起结构化辩论。3 轮辩论（Opening/Rebuttal/Closing）+ 投票 + 可选仲裁。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "topic": {"type": "string", "description": "辩论主题"},
      "participants": {"type": "array", "items": {"type": "string"}, "description": "参与辩论的智能体 ID 列表"},
      "initial_responses": {"type": "array", "items": {"type": "object"}, "description": "各方初始回答"},
      "voting_strategy": {"type": "string", "enum": ["majority", "weighted", "confidence_weighted", "consensus"], "default": "weighted"}
    },
    "required": ["topic", "participants", "initial_responses"]
  },
  "output": {
    "debate_id": "uuid",
    "rounds": "list[dict]",
    "votes": "dict",
    "final_resolution": "dict",
    "arbitration_needed": "boolean"
  }
}
```

### 10. call_external_agent（A2A 外部智能体调用）

**功能**：通过 A2A 协议调用别家厂商的智能体（[A2A-Protocol.md](../a2a/A2A-Protocol.md)）。

```json
{
  "name": "call_external_agent",
  "description": "通过 A2A 协议调用外部智能体。需用户提供数据共享同意。出口数据自动脱敏，返回结果校验诚信报告。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "to_agent_id": {"type": "string", "description": "目标外部 agent ID"},
      "capability_id": {"type": "string", "description": "调用的能力 ID（见 Agent Card）"},
      "input_data": {"type": "object", "description": "输入参数（自动脱敏 PII）"},
      "user_consent": {"type": "boolean", "description": "用户是否同意数据共享"}
    },
    "required": ["to_agent_id", "capability_id", "input_data", "user_consent"]
  },
  "output": {
    "task_id": "uuid",
    "state": "completed|failed|rejected",
    "result": "dict",
    "integrity_report": "dict",
    "integrity_verified": "boolean",
    "warning": "string|null"
  }
}
```

**安全约束**：
- 若 `user_consent` 为 false，拒绝调用
- 出口数据中 PII 字段（identifier/name/phone/address/account_number）自动脱敏
- 返回结果若缺少 `integrity_report`，标记为低信任并建议交叉验证

### 11. execute_reflexion（反思重试）

**功能**：子智能体/工具/转介调用失败时，执行反思-调整-重试（[Reflexion-Mechanism.md](../agents/Reflexion-Mechanism.md)）。

```json
{
  "name": "execute_reflexion",
  "description": "子智能体/工具/转介调用失败时的反思-调整-重试机制。MAX_RETRIES=3，失败后走 fallback。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "operation_type": {"type": "string", "enum": ["subagent", "tool", "transfer"]},
      "operation_name": {"type": "string", "description": "失败的操作名，如 'death-aftercare-emotional' 或 'query_knowledge'"},
      "failure_reason": {"type": "string", "description": "失败原因"},
      "original_input": {"type": "object", "description": "原始输入参数"}
    },
    "required": ["operation_type", "operation_name", "failure_reason", "original_input"]
  },
  "output": {
    "success": "boolean",
    "result": "dict|null",
    "attempts": "integer",
    "fallback_used": "boolean",
    "adjustments_applied": "list[str]",
    "reflexion_history": "list[dict]"
  }
}
```

**预定义调整策略**：见 [Reflexion-Mechanism.md](../agents/Reflexion-Mechanism.md) 的 `ADJUSTMENT_STRATEGIES` 表（10 种失败模式快速路径）。

### 12. init_transfer（转介初始化）

**功能**：智能体发起转介时，生成标准化的转介摘要。

```json
{
  "name": "init_transfer",
  "description": "初始化智能体间转介。生成标准化转介摘要（7字段），返回完整摘要供用户确认。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "from_agent": {"type": "string"},
      "to_agent": {"type": "string"},
      "reason": {"type": "string"},
      "user_situation": {"type": "string"},
      "confirmed_facts": {"type": "string"},
      "completed_items": {"type": "string", "description": "已办理的手续，若无则写'尚未办理任何手续'"},
      "current_question": {"type": "string"},
      "additional_context": {"type": "string"}
    },
    "required": ["from_agent", "to_agent", "reason", "user_situation", "current_question"]
  },
  "output": {
    "transfer_summary": {
      "转介自": "death-aftercare",
      "转介原因": "复杂财务",
      "用户情况": "北京/独生女/前天去世",
      "已确认": "无遗嘱/单独继承/多类资产",
      "已完成事项": "尚未办理任何手续",
      "当前问题": "资产清点与税务",
      "上下文传递": "已加载CN/overview.md"
    },
    "fields_complete": 7,
    "fields_missing": [],
    "transfer_id": "uuid",
    "user_confirmation_required": true
  }
}
```

### 13. report_incident（事故上报）

**功能**：上报事故到 accountability-framework 的事故记录系统。

```json
{
  "name": "report_incident",
  "description": "上报事故。当智能体发现错误（编造信息/违反规则/用户投诉）时调用。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "trace_id": {"type": "string"},
      "severity": {"type": "string", "enum": ["high", "medium", "low"]},
      "description": {"type": "string"},
      "root_cause_span_id": {"type": "string"},
      "rule_violation": {"type": "string"},
      "corrective_action": {"type": "string"},
      "user_notified": {"type": "boolean"}
    },
    "required": ["trace_id", "severity", "description"]
  },
  "output": {
    "incident_id": "uuid",
    "logged": true,
    "trace_file": "knowledge/_traces/.../trace_{uuid}.jsonl"
  }
}
```

## MCP Server 实现

### 用 FastMCP 实现（Python）

```python
# mcp_server/server.py（伪代码）
from fastmcp import FastMCP
import json
from pathlib import Path

mcp = FastMCP("deadman-platform")


@mcp.tool()
def check_rules(agent_name: str, output_text: str, context: dict = None) -> dict:
    """校验智能体输出是否符合规则"""
    rules_dir = Path("src/rules")
    violations = []
    warnings = []

    # 加载规则并校验
    for rule_file in rules_dir.glob("*.md"):
        rule_content = rule_file.read_text()
        # 正则 + 关键词 + LLM 三层校验
        # ...

    return {"passed": len(violations) == 0, "violations": violations, "warnings": warnings}


@mcp.tool()
def query_knowledge(
    country: str, region: str = None, topic: str = None, fallback_to_search: bool = True
) -> dict:
    """查询地域知识库"""
    knowledge_dir = Path(f"src/knowledge/regions/{country}")
    if region:
        target_file = knowledge_dir / f"{region}.md"
    else:
        target_file = knowledge_dir / "overview.md"

    if not target_file.exists():
        return {
            "found": False,
            "needs_research": True,
            "research_suggestion": f"建议触发policy-researcher搜索{country}/{region or '国家'}政策",
        }

    content = target_file.read_text()
    # 解析 frontmatter 获取 last_updated
    # ...

    return {
        "found": True,
        "data": {
            "content": content,
            "last_updated": "...",
            "sources": [],
            "trust_level": "high",
            "freshness_status": "fresh",
        },
    }


@mcp.tool()
def init_transfer(
    from_agent: str,
    to_agent: str,
    reason: str,
    user_situation: str,
    confirmed_facts: str,
    completed_items: str,
    current_question: str,
    additional_context: str = None,
) -> dict:
    """初始化转介"""
    summary = {
        "转介自": from_agent,
        "转介原因": reason,
        "用户情况": user_situation,
        "已确认": confirmed_facts,
        "已完成事项": completed_items,
        "当前问题": current_question,
        "上下文传递": additional_context or "",
    }
    # 校验 7 字段完整性
    fields_complete = sum(1 for v in summary.values() if v)
    fields_missing = [k for k, v in summary.items() if not v]

    return {
        "transfer_summary": summary,
        "fields_complete": fields_complete,
        "fields_missing": fields_missing,
        "transfer_id": str(uuid4()),
        "user_confirmation_required": True,
    }


# ... 其他工具

if __name__ == "__main__":
    mcp.run(transport="stdio")  # 或 "http" 用于远程
```

### 部署方式

#### 方式一：stdio（本地，最简单）

```json
// Claude Desktop / TRAE 的 MCP 配置
{
  "mcpServers": {
    "deadman-platform": {
      "command": "python",
      "args": ["src/mcp_server/server.py"],
      "cwd": "/workspace"
    }
  }
}
```

#### 方式二：HTTP（远程，多平台共享）

```bash
# 启动 MCP server
python src/mcp_server/server.py --transport http --port 8000
```

```json
// 各平台配置
{
  "mcpServers": {
    "deadman-platform": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## 平台适配简化

### 封装前（13 个平台各自实现）

```
TRAE: frontmatter tools 字段 + Read 工具读 rules/knowledge
OpenAI: function calling JSON Schema + 手动实现校验逻辑
Claude: tool_use 格式 + 手动实现
百炼: 插件 OpenAPI + 手动实现
... × 13
```

### 封装后（统一 MCP）

```
支持 MCP 的平台（Claude/OpenAI Agents SDK/TRAE/自建）：
  → 直接配置 MCP server，15 个工具自动可用
  
不支持 MCP 的平台（百炼/元宝/...）：
  → 适配层把 MCP 工具转换为平台原生格式
  → 一次适配，永久复用
```

## 与 agent.md 的关系

agent.md 的 tools 声明从直接声明工具改为声明 MCP server：

```yaml
# 封装前
tools: WebSearch, WebFetch, Read, Write

# 封装后
tools: WebSearch, WebFetch
mcp_servers:
  - deadman-platform  # 提供 check_rules, query_knowledge, init_transfer 等
```

智能体调用方式不变，只是工具来源从"平台原生"变为"MCP server 提供"。

## 安全考虑

1. **MCP server 不暴露文件系统**：只通过定义好的 15 个工具访问；read_file/write_file 受权限隔离约束，仅允许访问指定目录
2. **PII 脱敏**：所有经过 MCP server 的数据先脱敏
3. **权限隔离**：check_rules 只读 rules/，query_knowledge 只读 knowledge/，report_incident 只写 _incidents/
4. **审计日志**：每次 MCP 工具调用都记录 trace span

## 版本
- v1.4（v5.1.0）12 个工具迁移到 `@mcp.tool_auto` 装饰器（type hints + Google-style docstring 自动生成 JSON Schema），保留手写 schema：check_integrity / check_rules（嵌套对象 + 内部 enum）。工具总数仍为 15。详见 [CHANGELOG.md](../../CHANGELOG.md) v5.1.0 P8 章节
- v1.3（v4.6.1）新增 2 工具：`web_search_official`（仅返回官方源）+ `execute_code`（沙箱代码执行），工具总数 13 → 15
- v1.2 工具列表与 server.py 对齐为 13 工具：删除 accept_transfer/log_trace/get_confidence_label，新增 web_search/read_file/write_file/invoke_subagent；log_trace 由 observability tracer 自动上报替代
- v1.1 新增 4 工具（query_memory/initiate_debate/call_external_agent/execute_reflexion）+ query_knowledge 支持 LightRAG query_mode 和本体过滤 + check_integrity 集成 SelfCheckGPT
- v1.0 初始 MCP Server 方案（7 工具 + FastMCP 实现 + 部署方式）
