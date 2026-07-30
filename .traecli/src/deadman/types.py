"""核心数据类型 - 跨模块共享"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskTier(str, Enum):
    """风险等级 - risk-tier-framework.md"""

    R0 = "R0"  # 常规
    R1 = "R1"  # 注意
    R2 = "R2"  # 转介
    R3 = "R3"  # 安全优先


class ExecutionMode(str, Enum):
    """子智能体执行模式 - TEAM.md"""

    SUCCESS = "success"
    FALLBACK = "fallback"
    FAILED = "failed"


@dataclass
class TransferSummary:
    """转介摘要 - TEAM.md 定义的 7 字段"""

    from_agent: str
    to_agent: str
    reason: str
    user_situation: str
    current_question: str
    completed_items: list[str] = field(default_factory=list)
    pending_items: list[str] = field(default_factory=list)

    def is_complete(self) -> bool:
        """7 字段是否齐全"""
        return all(
            [
                self.from_agent,
                self.to_agent,
                self.reason,
                self.user_situation,
                self.current_question is not None,
            ]
        )


@dataclass
class SubagentResult:
    """子智能体返回结果 - TEAM.md 定义"""

    subagent_name: str
    execution_mode: ExecutionMode
    report: dict[str, Any]
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)


@dataclass
class RuleCheckResult:
    """规则校验结果"""

    passed: bool
    violations: list[dict[str, Any]] = field(default_factory=list)
    risk_tier: RiskTier = RiskTier.R0
    safety_triggered: bool = False
    integrity_violations: list[str] = field(default_factory=list)


@dataclass
class ConfidenceLabel:
    """置信度标注 - transparency-framework.md"""

    claim: str
    confidence: str  # 高/中/低/未知
    source: str | None = None
    reason: str | None = None
