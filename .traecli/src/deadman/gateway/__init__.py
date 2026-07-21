"""消息平台 Gateway 模块

借鉴 Hermes Agent (MIT License) 的 `gateway/run.py` 设计，但适配 deadman 身后事场景：
    - 入站消息直接响应（用户主动询问 = opt-in 当前会话）
    - 出站主动消息必须过 NotificationGuardrail
    - 不实现 Hermes 的 scale_to_zero / readiness / restart_loop_guard（deadman 是轻量部署）
    - 不实现 pairing 复杂流程，简化为 token 配对
"""

from __future__ import annotations

from .core import Gateway
from .connectors.base import PlatformConnector

__all__ = ["Gateway", "PlatformConnector"]
