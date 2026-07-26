"""投票策略 - 4 种独立可插拔的策略。

对应 agents/Debate-Voting.md 的伪代码，做最小必要实现：
- VotingStrategy：抽象基类，定义 vote() 接口
- MajorityVote：简单多数胜出
- WeightedVote：按 voter 身份加权（legal-advisor 1.2、arbiter 1.5 等）
- ConfidenceWeightedVote：把 position 自身 confidence 也纳入打分
- ConsensusVote：2/3 多数才算胜出，否则返回 needs_arbitration

每个策略 vote() 返回 dict，包含 winner/votes/strategy 与可选 needs_arbitration。
策略本身不调用 LLM，仅做票数统计；"voter 投给谁"由 orchestrator 决定。
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import DebatePosition


class VotingStrategy(Protocol):
    """投票策略接口 - 接收 positions + 已统计的 votes，返回裁决结果。

    votes 是 orchestrator 收集到的 {voter_id: voted_for_agent_id} 原始投票。
    策略基于此做加权/置信度/共识等运算。
    """

    name: str

    def vote(
        self,
        positions: list[DebatePosition],
        votes: dict[str, str],
    ) -> dict[str, Any]:
        ...


class MajorityVote:
    """简单多数胜出，平票时进入仲裁。"""

    name = "majority"

    def vote(
        self,
        positions: list[DebatePosition],
        votes: dict[str, str],
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for voted_for in votes.values():
            counts[voted_for] = counts.get(voted_for, 0) + 1
        if not counts:
            return {
                "winner": None,
                "votes": counts,
                "strategy": self.name,
                "needs_arbitration": True,
                "reason": "无有效投票",
            }
        max_v = max(counts.values())
        winners = [a for a, v in counts.items() if v == max_v]
        if len(winners) == 1:
            return {
                "winner": winners[0],
                "votes": counts,
                "strategy": self.name,
                "needs_arbitration": False,
            }
        return {
            "winner": None,
            "votes": counts,
            "strategy": self.name,
            "needs_arbitration": True,
            "reason": f"平票：{winners}",
        }


class WeightedVote:
    """按 voter 身份加权 - 不同 agent 投票权重不同。

    设计借鉴 Debate-Voting.md：
    - 法律/跨境问题权重高（1.2）
    - arbiter 权重最高（1.5）
    - 通用 agent 权重略低（0.9）
    """

    name = "weighted"

    VOTER_WEIGHTS: dict[str, float] = {
        "legal_advisor": 1.2,
        "cross_border_specialist": 1.2,
        "financial_analyst": 1.0,
        "policy_researcher": 1.1,
        "death_aftercare": 0.9,
        "medical_guide": 0.9,
        "debate_arbiter": 1.5,
    }

    def vote(
        self,
        positions: list[DebatePosition],
        votes: dict[str, str],
    ) -> dict[str, Any]:
        weighted: dict[str, float] = {}
        for voter, voted_for in votes.items():
            w = self.VOTER_WEIGHTS.get(voter, 1.0)
            weighted[voted_for] = weighted.get(voted_for, 0.0) + w
        if not weighted:
            return {
                "winner": None,
                "weighted_votes": weighted,
                "strategy": self.name,
                "needs_arbitration": True,
                "reason": "无有效投票",
            }
        max_s = max(weighted.values())
        winners = [a for a, s in weighted.items() if abs(s - max_s) < 1e-9]
        if len(winners) == 1:
            return {
                "winner": winners[0],
                "weighted_votes": weighted,
                "strategy": self.name,
                "needs_arbitration": False,
            }
        return {
            "winner": None,
            "weighted_votes": weighted,
            "strategy": self.name,
            "needs_arbitration": True,
            "reason": f"加权平票：{winners}",
        }


class ConfidenceWeightedVote:
    """置信度加权 - score = voter 权重 × position 自身 confidence。

    借鉴 Self-Consistency（Wang et al., 2022）：position 自身 confidence 高 + 多 voter 支持 → 高分。
    """

    name = "confidence_weighted"

    def vote(
        self,
        positions: list[DebatePosition],
        votes: dict[str, str],
    ) -> dict[str, Any]:
        # agent_id → position 索引
        pos_by_agent = {p.agent_id: p for p in positions}
        scores: dict[str, float] = {}
        for voter, voted_for in votes.items():
            voter_w = WeightedVote.VOTER_WEIGHTS.get(voter, 1.0)
            pos_conf = (
                pos_by_agent[voted_for].confidence
                if voted_for in pos_by_agent
                else 0.5
            )
            scores[voted_for] = scores.get(voted_for, 0.0) + voter_w * pos_conf
        if not scores:
            return {
                "winner": None,
                "scores": scores,
                "strategy": self.name,
                "needs_arbitration": True,
                "reason": "无有效投票",
            }
        max_s = max(scores.values())
        winners = [a for a, s in scores.items() if abs(s - max_s) < 1e-9]
        if len(winners) == 1:
            return {
                "winner": winners[0],
                "scores": scores,
                "strategy": self.name,
                "needs_arbitration": False,
            }
        return {
            "winner": None,
            "scores": scores,
            "strategy": self.name,
            "needs_arbitration": True,
            "reason": f"置信度加权平票：{winners}",
        }


class ConsensusVote:
    """共识投票 - 要求 2/3 多数才算胜出，否则进入仲裁。

    安全场景（风险分级 R2/R3）应优先用 consensus，避免边缘多数导致错误结论。
    """

    name = "consensus"
    THRESHOLD = 0.67

    def vote(
        self,
        positions: list[DebatePosition],
        votes: dict[str, str],
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for voted_for in votes.values():
            counts[voted_for] = counts.get(voted_for, 0) + 1
        total = sum(counts.values())
        if total == 0:
            return {
                "winner": None,
                "votes": counts,
                "strategy": self.name,
                "needs_arbitration": True,
                "reason": "无有效投票",
            }
        max_v = max(counts.values())
        # 2/3 阈值
        if max_v / total >= self.THRESHOLD:
            winner = max(counts, key=lambda a: counts[a])
            return {
                "winner": winner,
                "votes": counts,
                "strategy": self.name,
                "consensus_reached": True,
                "needs_arbitration": False,
            }
        return {
            "winner": None,
            "votes": counts,
            "strategy": self.name,
            "consensus_reached": False,
            "needs_arbitration": True,
            "reason": f"未达 2/3 阈值（{max_v}/{total}）",
        }


STRATEGY_REGISTRY: dict[str, type] = {
    "majority": MajorityVote,
    "weighted": WeightedVote,
    "confidence_weighted": ConfidenceWeightedVote,
    "consensus": ConsensusVote,
}


def get_strategy(name: str) -> VotingStrategy:
    """按名称获取投票策略实例。未知名降级到 weighted（与 mcp 默认一致）。"""
    cls = STRATEGY_REGISTRY.get(name, WeightedVote)
    return cls()  # type: ignore[return-value]
