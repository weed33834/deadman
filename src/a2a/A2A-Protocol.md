# A2A 协议适配

> 本文件定义如何用 Google A2A（Agent-to-Agent）Protocol 让我们的智能体与其他厂商的智能体互操作。借鉴 Google A2A Protocol（2025.04，50+ 厂商支持）、Anthropic MCP、OpenAI Function Calling、LangChain Agent Protocol。
>
> **目的**：当前 6 个并列智能体只能在我们的平台内协作。A2A 协议让我们的 death-aftercare 能调用别家厂商的律师 agent、税务 agent，反之别家的健康助手也能调用我们的 medical-guide。

## 为什么需要 A2A

### MCP vs A2A 的区别

```
MCP（Model Context Protocol）：
  LLM ↔ 工具/数据源
  解决"智能体如何调用外部工具"
  我们已实现：mcp_server/ 7 个工具

A2A（Agent-to-Agent Protocol）：
  Agent ↔ Agent
  解决"智能体如何调用其他智能体"
  我们需要：让 death-aftercare 能调用别家的律师 agent
```

### 当前痛点

```
用户：我爸在加州去世，涉及中美继承
我们的 cross-border-specialist：只能查我们自己的知识库
                                    ↓
                              加州政策可能不全
                                    ↓
                          若能调用加州当地的 legal agent
                          （通过 A2A）会更准确

反之：
别家健康助手：用户问"我爸住院了医保怎么报销"
            不懂异地医保
                ↓
            通过 A2A 调用我们的 medical-guide
            （它懂 CN 医保政策）
```

### A2A 补强

1. **Agent Card**：每个智能体发布"名片"，声明能力
2. **Task Lifecycle**：标准化的任务提交/执行/返回流程
3. **跨厂商互操作**：不绑定单一平台
4. **能力发现**：动态发现可用的外部 agent
5. **安全授权**：调用方需持有凭证

## A2A 协议核心概念

### 1. Agent Card（智能体名片）

```json
// a2a/agent_cards/death-aftercare.json
{
  "agent_id": "deadman-death-aftercare",
  "name": "身后事流程引导员",
  "description": "协助处理逝者身后事，包括死亡证明、户口注销、数字账号、遗产继承等 9 阶段全流程引导。专精中国大陆政策，部分覆盖美国加州、日本。",
  "version": "v4.1",
  "provider": {
    "name": "deadman Platform",
    "url": "https://aftercare.example.com",
    "contact": "support@aftercare.example.com"
  },
  "capabilities": [
    {
      "capability_id": "death-certificate-guidance",
      "name": "死亡证明办理引导",
      "description": "指导用户如何获取死亡证明，包括所需材料、办理地点、时限",
      "jurisdictions": ["CN", "US-CA", "JP"],
      "input_schema": {
        "type": "object",
        "properties": {
          "deceased_location": {"type": "object"},
          "death_cause": {"type": "string"},
          "applicant_relationship": {"type": "string"}
        }
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "procedure": {"type": "array"},
          "required_documents": {"type": "array"},
          "time_limit": {"type": "string"},
          "authority": {"type": "string"}
        }
      }
    },
    {
      "capability_id": "estate-inheritance-overview",
      "name": "遗产继承框架说明",
      "description": "提供法定继承顺序、份额的通用框架（不出法律意见）",
      "jurisdictions": ["CN"],
      "input_schema": {...},
      "output_schema": {...}
    },
    {
      "capability_id": "psychological-crisis-response",
      "name": "心理危机识别与应对",
      "description": "识别遗属的心理危机信号，提供专业资源引导",
      "jurisdictions": ["CN", "US", "JP"],
      "input_schema": {...},
      "output_schema": {...}
    }
  ],
  "integrity_guarantees": {
    "no_fabrication": true,
    "source_labeling": true,
    "confidence_labeling": true,
    "contradiction_detection": true
  },
  "security": {
    "authentication": "oauth2",
    "rate_limit": "100 requests/hour per client",
    "data_retention": "7 years (PIPL compliant)"
  },
  "languages": ["zh-CN", "en-US", "ja-JP"]
}
```

### 2. 6 个智能体的 Agent Card

```python
# a2a/agent_cards.py（伪代码）

AGENT_CARDS = {
    "death-aftercare": {
        "agent_id": "deadman-death-aftercare",
        "name": "身后事流程引导员",
        "capabilities": [
            "death-certificate-guidance",
            "estate-inheritance-overview",
            "psychological-crisis-response",
            "digital-account-succession",
            "household-cancellation",
            "funeral-service-guidance",
        ],
    },
    "legal-advisor": {
        "agent_id": "deadman-legal-advisor",
        "name": "法律顾问（不出法律意见）",
        "capabilities": [
            "inheritance-dispute-assessment",
            "lawyer-referral",
            "legal-framework-explanation",
            "statute-of-limitations-check",
        ],
    },
    "financial-analyst": {
        "agent_id": "deadman-financial-analyst",
        "name": "财务分析师",
        "capabilities": [
            "estate-asset-inventory",
            "tax-obligation-assessment",
            "insurance-claim-guidance",
            "debt-settlement-framework",
        ],
    },
    "policy-researcher": {
        "agent_id": "deadman-policy-researcher",
        "name": "政策研究员",
        "capabilities": [
            "policy-search",
            "policy-verification",
            "cross-jurisdiction-comparison",
            "policy-change-tracking",
        ],
    },
    "cross-border-specialist": {
        "agent_id": "deadman-cross-border-specialist",
        "name": "跨境专家",
        "capabilities": [
            "consular-authentication-guidance",
            "body-repatriation-framework",
            "legal-conflict-identification",
            "multi-jurisdiction-coordination",
        ],
    },
    "medical-guide": {
        "agent_id": "deadman-medical-guide",
        "name": "医疗导航员",
        "capabilities": [
            "medical-insurance-guidance",
            "hospital-information",
            "cross-region-medical-care",
            "medical-dispute-referral",
        ],
    },
}
```

## Task Lifecycle（任务生命周期）

```python
# a2a/task_lifecycle.py（伪代码）

from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any


class TaskState(str, Enum):
    SUBMITTED = "submitted"  # 已提交
    RECEIVED = "received"  # 已接收
    IN_PROGRESS = "in_progress"  # 处理中
    AWAITING_INPUT = "awaiting_input"  # 等待补充信息
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败
    REJECTED = "rejected"  # 拒绝（能力不匹配）


@dataclass
class A2ATask:
    """A2A 任务"""

    task_id: str
    from_agent_id: str  # 调用方
    to_agent_id: str  # 被调用方
    capability_id: str  # 调用的能力
    input_data: dict  # 输入参数
    state: TaskState
    created_at: datetime
    updated_at: datetime
    result: Optional[dict] = None
    error: Optional[str] = None
    integrity_report: Optional[dict] = None  # 诚信报告
    trace_span_id: Optional[str] = None  # OTel span


class A2AServer:
    """A2A 服务端 - 接收外部 agent 的调用"""

    def receive_task(self, task_request: dict) -> A2ATask:
        """接收外部 agent 的任务"""
        # 1. 验证调用方凭证
        if not self._authenticate(task_request["from_agent_id"], task_request["auth_token"]):
            return A2ATask(..., state=TaskState.REJECTED, error="authentication_failed")

        # 2. 能力匹配
        agent_card = self._get_agent_card(task_request["to_agent_id"])
        if task_request["capability_id"] not in agent_card.capabilities:
            return A2ATask(..., state=TaskState.REJECTED, error="capability_not_supported")

        # 3. 创建任务
        task = A2ATask(
            task_id=str(uuid4()),
            from_agent_id=task_request["from_agent_id"],
            to_agent_id=task_request["to_agent_id"],
            capability_id=task_request["capability_id"],
            input_data=task_request["input_data"],
            state=TaskState.RECEIVED,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            trace_span_id=start_span("a2a.receive", task_request),
        )

        # 4. 异步执行（走 LangGraph）
        asyncio.create_task(self._execute_task(task))
        return task

    async def _execute_task(self, task: A2ATask):
        """执行任务 - 走 LangGraph"""
        try:
            task.state = TaskState.IN_PROGRESS
            task.updated_at = datetime.utcnow()

            # 把 A2A 任务转为 LangGraph state
            state = self._convert_to_graph_state(task)

            # 调用主 graph
            graph = build_main_graph()
            result = await graph.ainvoke(state)

            # 提取结果
            task.result = {
                "response": result["final_response"],
                "confidence_labels": result.get("confidence_labels", []),
                "sources": self._extract_sources(result),
                "rule_check_passed": result.get("rule_check", {}).passed,
            }
            task.integrity_report = self._build_integrity_report(result)
            task.state = TaskState.COMPLETED

        except Exception as e:
            task.state = TaskState.FAILED
            task.error = str(e)

        finally:
            task.updated_at = datetime.utcnow()
            end_span(task.trace_span_id, task.state)
            self._notify_caller(task)

    def _build_integrity_report(self, result: dict) -> dict:
        """
        诚信报告 - 让调用方知道结果的可信度
        与 integrity-framework 集成
        """
        return {
            "no_fabrication_check": result.get("rule_check", {}).passed,
            "sources_provided": len(result.get("knowledge_results", [])) > 0,
            "confidence_labels_count": len(result.get("confidence_labels", [])),
            "single_source_warning": self._check_single_source(result),
            "integrity_check_passed": result.get("integrity_check_passed", False),
        }


class A2AClient:
    """A2A 客户端 - 调用外部 agent"""

    async def call_agent(self, to_agent_id: str, capability_id: str, input_data: dict) -> dict:
        """调用外部 agent"""
        # 1. 发现 agent endpoint
        endpoint = await self._discover_agent(to_agent_id)

        # 2. 提交任务
        task_request = {
            "from_agent_id": self.self_agent_id,
            "to_agent_id": to_agent_id,
            "capability_id": capability_id,
            "input_data": input_data,
            "auth_token": self._get_auth_token(to_agent_id),
        }
        task = await self._submit_task(endpoint, task_request)

        # 3. 轮询结果（或 webhook）
        result = await self._wait_for_completion(endpoint, task.task_id)

        # 4. 校验结果的诚信
        self._verify_integrity(result)

        return result
```

## Agent Discovery（能力发现）

```python
# a2a/discovery.py（伪代码）


class AgentDiscovery:
    """Agent 发现服务 - 类似服务注册中心"""

    REGISTRY_URL = "https://a2a-registry.example.com"

    async def discover_agents(self, capability: str, jurisdiction: str = None) -> list[dict]:
        """发现具备某能力的外部 agent"""
        params = {"capability": capability}
        if jurisdiction:
            params["jurisdiction"] = jurisdiction

        agents = await http_get(f"{self.REGISTRY_URL}/agents", params=params)

        # 按信誉、能力匹配度排序
        return sorted(agents, key=lambda a: (-a["reputation_score"], -a["capability_match_score"]))

    async def register_self(self, agent_card: dict):
        """注册自己的 agent 到发现服务"""
        await http_post(f"{self.REGISTRY_URL}/agents", agent_card)

    async def update_capability(self, agent_id: str, capabilities: list[str]):
        """更新能力声明"""
        await http_put(f"{self.REGISTRY_URL}/agents/{agent_id}", {"capabilities": capabilities})
```

## 转介机制的 A2A 映射

```python
# a2a/transfer_mapping.py

"""
现有的"转介"机制有两种：
1. 内部转介：death-aftercare → legal-advisor（我们平台内）
2. 外部转介：death-aftercare → 别家律师 agent（通过 A2A）

A2A 让外部转介成为可能。
"""


class HybridTransferManager:
    """混合转介管理 - 内部 + 外部"""

    async def init_transfer(self, transfer_summary: dict) -> dict:
        """发起转介"""
        to_agent = transfer_summary["to_agent"]

        # 1. 先检查是否是内部 agent
        if to_agent in INTERNAL_AGENTS:
            return await self._internal_transfer(transfer_summary)
        else:
            # 2. 外部 agent，走 A2A
            return await self._a2a_transfer(transfer_summary)

    async def _internal_transfer(self, summary: dict) -> dict:
        """内部转介 - 走 LangGraph conditional edge"""
        # 与 TEAM.md 定义的转介机制一致
        return {"transfer_type": "internal", "to_agent": summary["to_agent"]}

    async def _a2a_transfer(self, summary: dict) -> dict:
        """外部转介 - 走 A2A"""
        # 1. 发现外部 agent
        discovery = AgentDiscovery()
        external_agents = await discovery.discover_agents(
            capability=summary["capability_needed"], jurisdiction=summary["jurisdiction"]
        )

        if not external_agents:
            return {
                "transfer_type": "failed",
                "error": "no_external_agent_available",
                "fallback": "建议用户自行咨询当地专业人士",
            }

        # 2. 让用户选择
        options = [
            {"agent_id": a["agent_id"], "name": a["name"], "description": a["description"]}
            for a in external_agents[:3]
        ]

        return {
            "transfer_type": "external_a2a",
            "options": options,
            "user_confirmation_required": True,
            "data_sharing_consent_required": True,  # GDPR/PIPL
        }

    async def execute_a2a_transfer(self, user_choice: dict, transfer_summary: dict):
        """用户确认后，执行 A2A 转介"""
        # 1. 检查用户是否同意数据共享
        if not user_choice.get("data_sharing_consent"):
            return {"error": "data_sharing_consent_required"}

        # 2. 脱敏转介摘要（不传 PII）
        sanitized = self._sanitize_transfer_summary(transfer_summary)

        # 3. 调用外部 agent
        client = A2AClient()
        result = await client.call_agent(
            to_agent_id=user_choice["agent_id"],
            capability_id=user_choice["capability_id"],
            input_data=sanitized,
        )

        # 4. 校验外部 agent 返回的诚信报告
        if not result.get("integrity_report", {}).get("no_fabrication_check"):
            return {
                "warning": "external_agent_integrity_unverified",
                "result": result,
                "recommendation": "建议交叉验证外部 agent 的回答",
            }

        return result
```

## 安全与授权

```python
# a2a/security.py


class A2ASecurityManager:
    """A2A 安全管理"""

    async def authenticate_caller(self, from_agent_id: str, auth_token: str) -> bool:
        """验证调用方身份"""
        # OAuth2 token 验证
        return await oauth2_verify(from_agent_id, auth_token)

    async def authorize_capability(self, from_agent_id: str, capability_id: str) -> bool:
        """授权检查 - 调用方是否有权使用此能力"""
        # 基于 ACL 的权限检查
        acl = await self._get_acl(from_agent_id)
        return capability_id in acl.allowed_capabilities

    def sanitize_outgoing(self, data: dict) -> dict:
        """出口数据脱敏 - 不传 PII 给外部 agent"""
        PII_KEYS = {"identifier", "name", "phone", "address", "account_number"}
        return {k: "***" if k in PII_KEYS else v for k, v in data.items()}

    def verify_incoming_integrity(self, result: dict) -> dict:
        """校验外部 agent 返回结果的诚信"""
        report = result.get("integrity_report", {})

        checks = {
            "sources_provided": report.get("sources_provided", False),
            "no_fabrication_claimed": report.get("no_fabrication_check", False),
            "confidence_labeled": report.get("confidence_labels_count", 0) > 0,
        }

        # 若外部 agent 无诚信报告，标记为低信任
        if not report:
            checks["warning"] = "external_agent_no_integrity_report"

        return checks
```

## 与现有架构的集成

```
┌─────────────────────────────────────────────────────┐
│           用户（我们的平台）                         │
└────────────────────┬────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────┐
│         LangGraph 主 Graph                          │
│  ┌──────────────────────────────────────────────┐   │
│  │  6 个内部 agent node                         │   │
│  │  death-aftercare / legal-advisor / ...       │   │
│  └─────────────────┬────────────────────────────┘   │
│                    │                                │
│         ┌──────────┴───────────┐                    │
│         ↓                      ↓                    │
│  ┌──────────────┐      ┌──────────────┐             │
│  │ 内部转介     │      │ A2A 转介     │             │
│  │ (LangGraph   │      │ (A2AClient   │             │
│  │  edge)       │      │  → 外部)     │             │
│  └──────────────┘      └──────┬───────┘             │
│                               │                     │
└───────────────────────────────┼─────────────────────┘
                                ↓
                    ┌───────────────────────┐
                    │  A2A Registry         │
                    │  (能力发现)           │
                    └───────────┬───────────┘
                                ↓
        ┌───────────┬───────────┬───────────┐
        ↓           ↓           ↓           ↓
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │别家律师 │ │别家税务 │ │别家医疗 │ │别家跨境 │
   │ agent   │ │ agent   │ │ agent   │ │ agent   │
   └─────────┘ └─────────┘ └─────────┘ └─────────┘
```

## 平台无关性

A2A 协议是平台无关的，但不同平台对 A2A 的支持程度不同：

| 平台 | A2A 支持 | 适配方式 |
|------|---------|---------|
| TRAE | 原生支持 | 直接用 A2A SDK |
| Coze | 通过插件 | A2A 插件封装 |
| Dify | 通过 HTTP 节点 | HTTP 节点调用 A2A endpoint |
| OpenAI | 通过 Assistants API | custom function 调 A2A |
| Anthropic | 通过 tool use | tool 调 A2A |
| LangChain | 通过 LangGraph | 直接集成 A2A SDK |

## 评估指标

| 指标 | 目标 | 说明 |
|------|------|------|
| Agent Card 完整度 | 1.0 | 6 个 agent 的能力全部声明 |
| A2A 调用成功率 | ≥ 0.95 | 不含被拒（能力不匹配） |
| A2A 平均延迟 | ≤ 12s | 含外部 agent 处理时间 |
| 数据脱敏率 | 1.0 | 出口数据 100% 脱敏 |
| 外部结果诚信校验率 | 1.0 | 外部返回 100% 校验 |

## 版本

- v1.0 初始 A2A 协议适配方案（Agent Card + Task Lifecycle + Discovery + 内外部转介映射 + 安全授权）
```
