"""A2A v1.0 协议数据模型

定义 AgentCard / Task / TaskState / Message 等核心数据结构。
对齐 2026 v1.0 spec：AgentCard 含 skills 字段，Task 含 input-required 状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    """A2A 任务生命周期状态"""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class AgentCardSkill:
    """AgentCard 中的单个能力声明"""

    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    # v1.0 新增：能力适用地区/司法管辖区
    jurisdictions: list[str] = field(default_factory=list)


@dataclass
class AgentCard:
    """A2A v1.0 AgentCard - 智能体名片

    通过 GET /.well-known/agent.json 端点发布。
    """

    name: str
    description: str
    version: str
    url: str  # 本 agent 的 A2A endpoint
    skills: list[AgentCardSkill] = field(default_factory=list)
    provider: dict[str, str] = field(default_factory=dict)
    # v1.0：认证方式
    authentication: dict[str, Any] = field(default_factory=dict)
    # v1.0：能力范围声明（哪些能力需要用户同意）
    capabilities: dict[str, Any] = field(
        default_factory=lambda: {"streaming": True, "pushNotifications": False}
    )

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON-RPC 响应格式"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "url": self.url,
            "skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "tags": s.tags,
                    "inputSchema": s.input_schema,
                    "outputSchema": s.output_schema,
                    "jurisdictions": s.jurisdictions,
                }
                for s in self.skills
            ],
            "provider": self.provider,
            "authentication": self.authentication,
            "capabilities": self.capabilities,
        }


@dataclass
class A2ATask:
    """A2A 任务对象"""

    id: str
    state: TaskState = TaskState.SUBMITTED
    message: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    # v1.0：任务元数据
    metadata: dict[str, Any] = field(default_factory=dict)
    # 错误信息（state=failed 时）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "message": self.message,
            "result": self.result,
            "metadata": self.metadata,
            "error": self.error,
        }


@dataclass
class A2AMessage:
    """A2A 消息对象（任务输入/输出）"""

    role: str  # "user" / "agent"
    parts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "parts": self.parts}
