"""辩论数据模型 - 状态机 + 立场 + 会话。

对应 agents/Debate-Voting.md 的伪代码 dataclass，做最小必要实现：
- DebateState：状态枚举（INITIATED → OPENING → REBUTTAL → CLOSING → VOTING → ARBITRATION → CONCLUDED）
- DebatePosition：单个参与方的立场 + 证据 + 置信度
- Debate：辩论会话聚合根，包含 rounds/votes/final_resolution

刻意保留为可序列化 dict 形式，便于 trace span 序列化与持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class DebateState(str, Enum):
    """辩论状态机 - 7 态。

    INITIATED → OPENING → REBUTTAL → CLOSING → VOTING →（平票）→ ARBITRATION → CONCLUDED
                                            ↓
                                       （明确胜出）→ CONCLUDED
    """

    INITIATED = "initiated"
    OPENING = "opening"
    REBUTTAL = "rebuttal"
    CLOSING = "closing"
    VOTING = "voting"
    ARBITRATION = "arbitration"
    CONCLUDED = "concluded"


@dataclass
class DebatePosition:
    """单个参与方的立场 - 在辩论过程中可被修正。

    Attributes:
        agent_id: 智能体 ID（与 agents/*.md 文件名对应，下划线↔短横线）
        position: 该 agent 的主张（一句话结论）
        supporting_evidence: 支持证据列表，每条 {"type","content","source"}
        confidence: 自评估置信度 0.0-1.0
        jurisdiction_basis: 法律/政策依据（可选，跨境场景常用）
    """

    agent_id: str
    position: str
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.7
    jurisdiction_basis: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "position": self.position,
            "supporting_evidence": self.supporting_evidence,
            "confidence": self.confidence,
            "jurisdiction_basis": self.jurisdiction_basis,
        }


@dataclass
class Debate:
    """辩论会话聚合根。

    Attributes:
        debate_id: UUID
        topic: 辩论主题（用户原问题）
        participants: 参与 agent_id 列表
        positions: 各方立场（在 closing 阶段可能被修正）
        rounds: 辩论轮次记录 [{round, agent, type, statement}]
        votes: {voter_id: voted_for_agent}
        final_resolution: 最终裁决（投票或仲裁产生）
        state: 当前状态
        created_at / updated_at: 时间戳
        token_used: 累计 token 消耗（预算控制用）
    """

    debate_id: str
    topic: str
    participants: list[str]
    positions: list[DebatePosition]
    rounds: list[dict[str, Any]] = field(default_factory=list)
    votes: dict[str, str] = field(default_factory=dict)
    final_resolution: dict[str, Any] | None = None
    state: DebateState = DebateState.INITIATED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    token_used: int = 0

    def touch(self) -> None:
        """更新 updated_at 时间戳"""
        self.updated_at = datetime.now().isoformat()

    def add_round(
        self,
        round_num: int,
        agent_id: str,
        round_type: str,
        statement: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """追加一轮辩论记录"""
        entry: dict[str, Any] = {
            "round": round_num,
            "agent": agent_id,
            "type": round_type,
            "statement": statement,
        }
        if extra:
            entry.update(extra)
        self.rounds.append(entry)
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        return {
            "debate_id": self.debate_id,
            "topic": self.topic,
            "participants": self.participants,
            "positions": [p.to_dict() for p in self.positions],
            "rounds": self.rounds,
            "votes": self.votes,
            "final_resolution": self.final_resolution,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_used": self.token_used,
        }
