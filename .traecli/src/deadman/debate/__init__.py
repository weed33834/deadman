"""deadman.debate - 多智能体辩论/投票模块

实现 .traecli/agents/Debate-Voting.md 设计：
- 3 轮辩论（Opening/Rebuttal/Closing）
- 4 种投票策略（majority/weighted/confidence_weighted/consensus）
- 仲裁机制（投票不分胜负时介入）
- 诚信约束（integrity-framework 强制注入）
- trace span 集成（debate.session → round.* → voting/arbitration）
- 预算控制与收敛检测（避免无限辩论与 token 爆炸）

公开接口：
- DebateState：辩论状态枚举
- DebatePosition / Debate：数据模型
- DebateOrchestrator：辩论编排器（mcp_server.initiate_debate 调用）
- VotingStrategy / MajorityVote / WeightedVote / ConfidenceWeightedVote / ConsensusVote
"""

from .models import Debate, DebatePosition, DebateState
from .orchestrator import DebateOrchestrator
from .voting import (
    ConfidenceWeightedVote,
    ConsensusVote,
    MajorityVote,
    VotingStrategy,
    WeightedVote,
)

__all__ = [
    "Debate",
    "DebateOrchestrator",
    "DebatePosition",
    "DebateState",
    "VotingStrategy",
    "MajorityVote",
    "WeightedVote",
    "ConfidenceWeightedVote",
    "ConsensusVote",
]
