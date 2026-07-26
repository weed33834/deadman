"""DebateOrchestrator 单元测试 - 覆盖 5 个核心场景

场景设计：
1. 简单多数胜出（2 个 agent，一个投票多）
2. 加权投票 - 不同权重影响胜出
3. 平票进仲裁
4. 收敛检测 - rebuttal 含让步词提前结束
5. token 预算超限降级仲裁
6. LLM 不可用降级（不抛异常）

使用 MockLLMClient 模拟 LLM 调用，避免真实 API 依赖。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.debate import (
    ConsensusVote,
    Debate,
    DebateOrchestrator,
    DebatePosition,
    DebateState,
    MajorityVote,
    WeightedVote,
    ConfidenceWeightedVote,
)
from deadman.debate.voting import get_strategy


# =================================================================
# Mock LLM Client - 模拟 chat / chat_json / last_usage
# =================================================================


class MockLLMClient:
    """模拟 LLM 客户端 - 按 chat_mode 返回预设响应

    chat_mode:
        "normal" - chat 返回模拟陈述文本
        "json" - chat_json 返回模拟 dict
        "raise" - 抛异常（测试降级）
        "empty" - 返回空字符串
    """

    def __init__(self, mode: str = "normal", api_key: str = "mock-key") -> None:
        self.mode = mode
        self.api_key = api_key
        self.last_usage: dict[str, int] = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        self.call_count = 0

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3, **kwargs: Any) -> str:
        self.call_count += 1
        if self.mode == "raise":
            raise RuntimeError("mock LLM error")
        if self.mode == "empty":
            return ""
        # 根据消息内容返回不同模拟陈述
        content = messages[0]["content"] if messages else ""
        if "Opening" in content or "陈述你的立场" in content:
            return f"基于法规 XXX，我的立场是：适用中国法律。来源：民法典第 1123 条。"
        if "反驳其他参与方" in content or "交叉质询" in content:
            return "对方引用法规有误。我承认对方部分论点合理。但结论应坚持中国法。"
        if "总结你的最终立场" in content or "总结陈词" in content:
            return '{"final_position":"坚持适用中国法","confidence":0.85,"concessions_made":["承认对方部分论点"],"key_evidence":["民法典第1123条"]}'
        if "提取核心立场" in content:
            return "适用中国法律"
        return "模拟陈述文本"

    async def chat_json(self, messages: list[dict[str, str]], temperature: float = 0.3, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        if self.mode == "raise":
            raise RuntimeError("mock LLM error")
        if self.mode == "empty":
            return {}
        content = messages[0]["content"] if messages else ""
        if "总结你的最终立场" in content:
            return {
                "final_position": "坚持适用中国法",
                "confidence": 0.85,
                "concessions_made": ["承认对方部分论点"],
                "key_evidence": ["民法典第1123条"],
            }
        if "投票支持" in content:
            return {
                "vote_for": "cross_border_specialist",
                "reason": "论据更充分",
                "confidence": 0.8,
            }
        if "最终裁决" in content:
            return {
                "resolution": "综合判断适用中国法，但建议咨询律师",
                "confidence": 0.7,
                "key_disagreement": "不动产所在地法 vs 本国法",
                "professional_referral_needed": True,
                "referral_target": "lawyer",
            }
        return {}


# =================================================================
# 测试 1: 简单多数胜出
# =================================================================


@pytest.mark.asyncio
async def test_majority_vote_winner():
    """2 个 agent 辩论，cross_border_specialist 获胜"""
    llm = MockLLMClient()
    orchestrator = DebateOrchestrator(
        llm_client=llm,  # type: ignore[arg-type]
        voting_strategy="majority",
        max_token_budget=100000,  # 不触发预算降级
    )

    result = await orchestrator.run_debate(
        topic="中美跨境继承适用哪国法律？",
        participants=["cross_border_specialist", "legal_advisor"],
        initial_responses=[
            {
                "agent": "cross_border_specialist",
                "response": "适用中国法律（被继承人本国法）",
                "confidence": 0.8,
            },
            {
                "agent": "legal_advisor",
                "response": "适用不动产所在地法（加州房产适用加州法）",
                "confidence": 0.6,
            },
        ],
    )

    # 基本结构断言
    assert result["not_implemented"] is False
    assert result["topic"] == "中美跨境继承适用哪国法律？"
    assert len(result["participants"]) == 2
    assert len(result["rounds"]) > 0
    assert result["voting_strategy"] == "majority"
    # 由于 MockLLMClient 一律 vote_for=cross_border_specialist，必胜
    assert result["final_resolution"] is not None
    assert result["final_resolution"]["winner"] == "cross_border_specialist"
    assert result["arbitration_needed"] is False
    # token 累计 > 0（多次 LLM 调用）
    assert result["token_used"] > 0


# =================================================================
# 测试 2: 加权投票策略
# =================================================================


def test_weighted_vote_strategy():
    """测试 WeightedVote 策略 - 不同 voter 权重影响结果"""
    positions = [
        DebatePosition(agent_id="agent_a", position="立场 A", confidence=0.5),
        DebatePosition(agent_id="agent_b", position="立场 B", confidence=0.9),
    ]
    # agent_a 拿到 2 票（普通 agent + financial），agent_b 拿到 1 票（arbiter 加权 1.5）
    votes = {
        "death_aftercare": "agent_a",  # 0.9
        "financial_analyst": "agent_a",  # 1.0 → agent_a 总 1.9
        "debate_arbiter": "agent_b",  # 1.5
    }
    strategy = WeightedVote()
    result = strategy.vote(positions, votes)

    # agent_a 加权 1.9 vs agent_b 1.5 → agent_a 胜
    assert result["winner"] == "agent_a"
    assert result["needs_arbitration"] is False
    assert result["strategy"] == "weighted"


def test_weighted_vote_tie_triggers_arbitration():
    """平票触发仲裁"""
    positions = [
        DebatePosition(agent_id="agent_a", position="立场 A"),
        DebatePosition(agent_id="agent_b", position="立场 B"),
    ]
    # 两个都是 arbiter 投票，权重相同 → 平票
    votes = {"arbiter1": "agent_a", "arbiter2": "agent_b"}
    strategy = WeightedVote()
    result = strategy.vote(positions, votes)
    assert result["winner"] is None
    assert result["needs_arbitration"] is True


# =================================================================
# 测试 3: 共识投票阈值
# =================================================================


def test_consensus_vote_below_threshold():
    """共识投票 - 低于 2/3 阈值进入仲裁"""
    positions = [
        DebatePosition(agent_id="agent_a", position="立场 A"),
        DebatePosition(agent_id="agent_b", position="立场 B"),
        DebatePosition(agent_id="agent_c", position="立场 C"),
    ]
    # 2/3 之 1 票，未达阈值
    votes = {"voter1": "agent_a", "voter2": "agent_b", "voter3": "agent_c"}
    strategy = ConsensusVote()
    result = strategy.vote(positions, votes)
    assert result["consensus_reached"] is False
    assert result["needs_arbitration"] is True


def test_consensus_vote_above_threshold():
    """共识投票 - 达 2/3 阈值（用 4 票中 3 票，0.75 > 0.67）"""
    positions = [
        DebatePosition(agent_id="agent_a", position="立场 A"),
        DebatePosition(agent_id="agent_b", position="立场 B"),
    ]
    # 4 票中 3 票给 agent_a → 0.75 ≥ 0.67 达阈值
    votes = {
        "voter1": "agent_a",
        "voter2": "agent_a",
        "voter3": "agent_a",
        "voter4": "agent_b",
    }
    strategy = ConsensusVote()
    result = strategy.vote(positions, votes)
    assert result["consensus_reached"] is True
    assert result["winner"] == "agent_a"


# =================================================================
# 测试 4: ConfidenceWeightedVote
# =================================================================


def test_confidence_weighted_vote():
    """置信度加权 - 高 confidence 的 position 即使投票少也可能胜"""
    positions = [
        DebatePosition(agent_id="agent_a", position="立场 A", confidence=0.3),
        DebatePosition(agent_id="agent_b", position="立场 B", confidence=0.95),
    ]
    # agent_a 拿 2 票（普通权重 0.9 + 1.0），agent_b 拿 1 票（arbiter 1.5）
    # agent_a score = 0.9*0.3 + 1.0*0.3 = 0.57
    # agent_b score = 1.5*0.95 = 1.425
    # agent_b 胜
    votes = {
        "death_aftercare": "agent_a",
        "financial_analyst": "agent_a",
        "debate_arbiter": "agent_b",
    }
    strategy = ConfidenceWeightedVote()
    result = strategy.vote(positions, votes)
    assert result["winner"] == "agent_b"
    assert result["needs_arbitration"] is False


# =================================================================
# 测试 5: token 预算超限降级
# =================================================================


@pytest.mark.asyncio
async def test_token_budget_degradation():
    """token 预算超限 → 跳过 rebuttal/closing 直入投票"""
    llm = MockLLMClient()
    # 设极小预算，第一次 LLM 调用就超限
    orchestrator = DebateOrchestrator(
        llm_client=llm,  # type: ignore[arg-type]
        voting_strategy="majority",
        max_token_budget=10,  # 极小预算
    )

    result = await orchestrator.run_debate(
        topic="测试主题",
        participants=["agent_a", "agent_b"],
        initial_responses=[
            {"agent": "agent_a", "response": "立场 A"},
            {"agent": "agent_b", "response": "立场 B"},
        ],
    )

    # 应该能跑完不抛异常
    assert result["not_implemented"] is False
    # 由于预算超限，closing 可能没有 round
    round_types = {r.get("type") for r in result["rounds"]}
    # 至少有 opening
    assert "opening" in round_types


# =================================================================
# 测试 6: LLM 不可用降级（不抛异常）
# =================================================================


@pytest.mark.asyncio
async def test_llm_unavailable_degradation():
    """LLM API key 为空 → 全程降级，不抛异常"""
    llm = MockLLMClient(api_key="")  # 无 key
    orchestrator = DebateOrchestrator(
        llm_client=llm,  # type: ignore[arg-type]
        voting_strategy="weighted",
    )

    result = await orchestrator.run_debate(
        topic="测试主题",
        participants=["agent_a", "agent_b"],
        initial_responses=[
            {"agent": "agent_a", "response": "立场 A"},
            {"agent": "agent_b", "response": "立场 B"},
        ],
    )

    # 不抛异常，返回结构化结果
    assert result["not_implemented"] is False
    assert "debate_id" in result
    assert "rounds" in result


# =================================================================
# 测试 7: LLM 异常时降级（不抛）
# =================================================================


@pytest.mark.asyncio
async def test_llm_raise_degradation():
    """LLM 抛异常 → 降级返回，不抛"""
    llm = MockLLMClient(mode="raise")
    orchestrator = DebateOrchestrator(
        llm_client=llm,  # type: ignore[arg-type]
        voting_strategy="majority",
    )

    result = await orchestrator.run_debate(
        topic="测试主题",
        participants=["agent_a", "agent_b"],
        initial_responses=[
            {"agent": "agent_a", "response": "立场 A"},
            {"agent": "agent_b", "response": "立场 B"},
        ],
    )

    # 不抛异常，返回结构化结果
    assert result["not_implemented"] is False


# =================================================================
# 测试 8: 模型基础测试 - Debate / DebatePosition
# =================================================================


def test_debate_position_to_dict():
    """DebatePosition 序列化为 dict"""
    pos = DebatePosition(
        agent_id="legal_advisor",
        position="适用加州法",
        supporting_evidence=[{"type": "statute", "content": "加州继承法", "source": "..."}],
        confidence=0.8,
        jurisdiction_basis="California Probate Code",
    )
    d = pos.to_dict()
    assert d["agent_id"] == "legal_advisor"
    assert d["position"] == "适用加州法"
    assert d["confidence"] == 0.8
    assert len(d["supporting_evidence"]) == 1
    assert d["jurisdiction_basis"] == "California Probate Code"


def test_debate_add_round():
    """Debate.add_round 追加记录"""
    debate = Debate(
        debate_id="test-1",
        topic="测试主题",
        participants=["agent_a", "agent_b"],
        positions=[
            DebatePosition(agent_id="agent_a", position="立场 A"),
            DebatePosition(agent_id="agent_b", position="立场 B"),
        ],
    )
    debate.add_round(1, "agent_a", "opening", "我的立场是...")
    assert len(debate.rounds) == 1
    assert debate.rounds[0]["round"] == 1
    assert debate.rounds[0]["type"] == "opening"
    assert debate.updated_at >= debate.created_at


def test_debate_state_machine():
    """DebateState 枚举值"""
    assert DebateState.INITIATED.value == "initiated"
    assert DebateState.CONCLUDED.value == "concluded"


# =================================================================
# 测试 9: get_strategy 工厂
# =================================================================


def test_get_strategy_known():
    """get_strategy 已知名返回对应类"""
    assert isinstance(get_strategy("majority"), MajorityVote)
    assert isinstance(get_strategy("weighted"), WeightedVote)
    assert isinstance(get_strategy("consensus"), ConsensusVote)
    assert isinstance(get_strategy("confidence_weighted"), ConfidenceWeightedVote)


def test_get_strategy_unknown_fallback():
    """get_strategy 未知名降级到 WeightedVote"""
    strategy = get_strategy("nonexistent")
    assert isinstance(strategy, WeightedVote)


# =================================================================
# 测试 10: 收敛检测
# =================================================================


def test_convergence_detection():
    """收敛检测 - rebuttal 含让步词且达半数 → 触发"""
    orchestrator = DebateOrchestrator(
        llm_client=MockLLMClient(),  # type: ignore[arg-type]
    )
    debate = Debate(
        debate_id="test",
        topic="测试",
        participants=["agent_a", "agent_b"],
        positions=[
            DebatePosition(agent_id="agent_a", position="A"),
            DebatePosition(agent_id="agent_b", position="B"),
        ],
    )
    # 全部 rebuttal 含让步词
    debate.add_round(2, "agent_a", "rebuttal", "我承认对方论点合理")
    debate.add_round(2, "agent_b", "rebuttal", "我同意部分观点")
    assert orchestrator._is_converged(debate) is True


def test_convergence_detection_no_concession():
    """收敛检测 - rebuttal 无让步词 → 不触发"""
    orchestrator = DebateOrchestrator(
        llm_client=MockLLMClient(),  # type: ignore[arg-type]
    )
    debate = Debate(
        debate_id="test",
        topic="测试",
        participants=["agent_a", "agent_b"],
        positions=[
            DebatePosition(agent_id="agent_a", position="A"),
            DebatePosition(agent_id="agent_b", position="B"),
        ],
    )
    debate.add_round(2, "agent_a", "rebuttal", "完全错误，对方观点不成立")
    debate.add_round(2, "agent_b", "rebuttal", "我方立场不变")
    assert orchestrator._is_converged(debate) is False
