"""A2A v1.0 / v1.2 协议数据模型

定义 AgentCard / Task / TaskState / Message 等核心数据结构。
对齐 2026 v1.0 spec：AgentCard 含 skills 字段，Task 含 input-required 状态。
P4.4 v1.2 扩展（feature flag DEADMAN_A2A_V12_ENABLED=0 默认关闭）：
- AgentCard.capabilities.pushNotifications 可开启
- PushNotificationConfig dataclass（webhook 推送配置）
- 仅 v1.2 开启时启用新字段，v1.0 行为完全不变
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# =====================================================================
# P4.4 Feature flag - 默认关闭，保证 v1.0 行为不变
# =====================================================================
A2A_V12_ENABLED: bool = os.environ.get(
    "DEADMAN_A2A_V12_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")


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
class PushNotificationConfig:
    """P4.4 v1.2 Webhook 推送配置

    用于 tasks/sendPush 方法 - 把任务状态变更主动推送到调用方 webhook。
    仅在 DEADMAN_A2A_V12_ENABLED=1 时生效；v1.0 客户端不会看到此字段。

    Attributes:
        url: webhook 接收端 URL
        token: bearer token（推送到 webhook 时放 Authorization header）
        event_types: 订阅的事件类型（如 ["task.completed", "task.failed"]）；
                     空表示订阅全部事件
    """

    url: str
    token: str = ""
    event_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "token": self.token,
            "event_types": list(self.event_types),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PushNotificationConfig":
        if not isinstance(data, dict):
            return cls(url="")
        return cls(
            url=str(data.get("url", "")),
            token=str(data.get("token", "")),
            event_types=list(data.get("event_types", []) or []),
        )


@dataclass
class AgentCard:
    """A2A v1.0 AgentCard - 智能体名片

    通过 GET /.well-known/agent.json 端点发布。

    P4.4 v1.2 扩展（feature flag 控制）：
    - capabilities.pushNotifications：v1.2 开启时为 True，否则保持 v1.0 默认 False
    - push_notification_config：v1.2 开启时可附加 PushNotificationConfig
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
    # P4.4 v1.2：pushNotifications 在 v1.2 开启时为 True
    # default_factory 在实例化时读 A2A_V12_ENABLED，保证 v1.0=False / v1.2=True
    capabilities: dict[str, Any] = field(
        default_factory=lambda: {
            "streaming": True,
            "pushNotifications": bool(A2A_V12_ENABLED),
        }
    )
    # P4.4 v1.2：webhook 推送配置（仅 v1.2 开启时序列化）
    push_notification_config: PushNotificationConfig | None = None

    def __post_init__(self) -> None:
        """v1.2 开启时确保 capabilities.pushNotifications / streaming 为 True

        default_factory 已按 A2A_V12_ENABLED 设置默认值；
        __post_init__ 仅在调用方传入的 capabilities 缺失这两个 key 时补默认值
        （setdefault 不覆盖已有 key，保留调用方显式设置）。
        """
        if A2A_V12_ENABLED:
            caps = dict(self.capabilities) if self.capabilities else {}
            # 仅在 key 缺失时补默认 True（不覆盖调用方显式设置的值）
            if "pushNotifications" not in caps:
                caps["pushNotifications"] = True
            if "streaming" not in caps:
                caps["streaming"] = True
            self.capabilities = caps

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON-RPC 响应格式

        v1.2 开启时附加 pushNotificationConfig 字段；v1.0 模式下保持原样。
        """
        result = {
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
        # v1.2 扩展字段
        if A2A_V12_ENABLED and self.push_notification_config is not None:
            result["pushNotificationConfig"] = self.push_notification_config.to_dict()
        return result


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
