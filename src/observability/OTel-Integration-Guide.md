# OpenTelemetry GenAI 接入指南

> 本文件定义如何用 OpenTelemetry GenAI Semantic Conventions 为平台接入可观测性。借鉴 OTel GenAI conventions（2024）、OpenInference（Arize）、Langfuse 的 OTel 兼容设计。

## 为什么用 OTel GenAI

1. **标准统一**：13 个平台适配时 trace 格式一致
2. **工具链复用**：可导入 Jaeger/Grafana/Datadog/Langfuse/Phoenix
3. **跨语言**：Python/Node.js/Go/Java 都有 SDK
4. **未来-proof**：OTel 是 CNCF 顶级项目，行业标准

## OTel GenAI 标准属性

OTel GenAI Semantic Conventions 定义了以下标准属性（我们直接复用）：

### 通用属性

| 属性 | 说明 | 我们的值 |
|------|------|---------|
| `gen_ai.system` | LLM 提供商 | `openai`/`anthropic`/`trae`/`aliyun`/... |
| `gen_ai.request.model` | 模型名 | `gpt-4o`/`claude-3-5-sonnet`/`glm-4.6`/... |
| `gen_ai.request.max_tokens` | 最大 token | - |
| `gen_ai.request.temperature` | 温度 | - |
| `gen_ai.usage.prompt_tokens` | 输入 token | - |
| `gen_ai.usage.completion_tokens` | 输出 token | - |
| `gen_ai.response.id` | 响应 ID | - |
| `gen_ai.response.finish_reason` | 结束原因 | `stop`/`tool_calls`/`length` |

### 工具调用属性

| 属性 | 说明 |
|------|------|
| `gen_ai.tool.name` | 工具名 |
| `gen_ai.tool.description` | 工具描述 |
| `gen_ai.tool.call.id` | 调用 ID |

## 平台扩展属性

OTel GenAI 标准不覆盖多智能体场景，我们扩展以下属性（前缀 `agent.`）：

### Agent 扩展

| 属性 | 说明 | 对应 span_type |
|------|------|---------------|
| `agent.name` | 智能体名 | agent/subagent |
| `agent.role` | 角色 | agent |
| `agent.entry_mode` | direct/transfer | agent |
| `agent.transfer_summary_fields_complete` | 转介摘要完整字段数 | agent |
| `agent.rules_triggered` | 触发的规则 | agent |
| `agent.integrity_self_check_passed` | 5 关自检通过 | agent |
| `agent.confidence_labels` | 置信度标注 | agent |
| `agent.sources_cited` | 引用来源 | agent |

### Transfer 扩展

| 属性 | 说明 |
|------|------|
| `transfer.from_agent` | 转介源 |
| `transfer.to_agent` | 转介目标 |
| `transfer.reason` | 转介原因 |
| `transfer.user_confirmed` | 用户确认 |
| `transfer.cross_team` | 跨团队 |

### Rule 扩展

| 属性 | 说明 |
|------|------|
| `rule.file` | 规则文件 |
| `rule.priority_level` | L0-L8 |
| `rule.chapter` | 章节 |
| `rule.conflict_detected` | 冲突检测 |
| `rule.resolution` | 裁决结果 |
| `rule.deferred` | 是否延后 |

## 接入方式（按平台）

### 方式一：原生 OTel SDK（推荐）

适用于有代码控制权的平台（自建、LangGraph、AutoGen 后端）：

```python
# 伪代码
from opentelemetry import trace
from opentelemetry.trace import SpanKind

tracer = trace.get_tracer("deadman")


def handle_user_request(user_input, user_id, platform):
    with tracer.start_as_current_span("user_request", kind=SpanKind.SERVER) as root:
        root.set_attribute("user_id_hash", hash(user_id))
        root.set_attribute("platform", platform)
        root.set_attribute("user_input_pii_redacted", True)

        # 调用 death-aftercare
        with tracer.start_as_current_span("agent.death-aftercare") as agent_span:
            agent_span.set_attribute("agent.name", "death-aftercare")
            agent_span.set_attribute("agent.entry_mode", "direct")
            # ... 智能体执行
            agent_span.set_attribute("agent.rules_triggered", ["L1.integrity", "L4.risk-tier"])
            agent_span.set_attribute("agent.integrity_self_check_passed", True)
```

### 方式二：Langfuse SDK（自部署）

适用于想要开箱即用可观测性的场景：

```python
# 伪代码
from langfuse import Langfuse
from langfuse.openai import openai  # 自动 instrument OpenAI 调用

langfuse = Langfuse(host="http://localhost:3000", public_key="...")

trace = langfuse.trace(name="user_request", user_id="hash...")
generation = trace.generation(name="agent.death-aftercare", model="gpt-4o", input=...)
# ... 智能体执行
generation.end(output=..., usage={"prompt_tokens": 100, "completion_tokens": 200})
```

Langfuse 原生支持 OTel 协议，可作为 OTel 的 backend。

### 方式三：平台原生 tracing（TRAE/OpenAI Agents SDK）

部分平台有原生 tracing，直接复用：

- **TRAE**：Subagent 调用有原生 trace
- **OpenAI Agents SDK**：内置 OpenTelemetry tracing
- **Bedrock Agents**：内置 CloudWatch trace
- **Vertex AI**：内置 Cloud Trace

适配层把这些原生 trace 转换为我们的 span 模型。

## Langfuse 自部署方案

### 部署架构

```
用户 → 平台（TRAE/OpenAI/...）→ LLM API
                ↓
         OTel/Langfuse SDK
                ↓
         Langfuse（自部署）
         ├── PostgreSQL（trace 存储）
         ├── ClickHouse（分析）
         └── Web Dashboard
```

### Docker Compose 部署

```yaml
# docker-compose.yml（伪代码，实际部署参考 Langfuse 官方文档）
version: '3'
services:
  langfuse:
    image: langfuse/langfuse:latest
    ports: ["3000:3000"]
    environment:
      - DATABASE_URL=postgresql://...
      - NEXTAUTH_SECRET=...
    depends_on: [postgres, clickhouse]
  postgres:
    image: postgres:15
  clickhouse:
    image: clickhouse/clickhouse-server:latest
```

### 数据合规

- **自部署**：数据不出本地，满足 PIPL/GDPR
- **PII 脱敏**：trace 写入前脱敏（在 SDK 层处理）
- **留存期限**：trace 默认留存 90 天，可配置
- **删除权**：用户可请求删除自己的 trace（GDPR/PIPL 权利）

## 跨平台一致性

所有平台适配层必须输出相同格式的 span，确保：

1. **span_type 一致**：6 类 span 在所有平台都有
2. **属性名一致**：用 OTel GenAI 标准 + 我们的 `agent.*`/`transfer.*`/`rule.*` 扩展
3. **trace_id 串联**：跨平台转介时（如 TRAE 的 death-aftercare 转给 Vertex AI 的 medical-guide）trace_id 保持一致

## 版本
- v1.0 初始 OTel 接入指南
