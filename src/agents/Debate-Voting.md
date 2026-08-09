# 辩论/投票机制

> 本文件定义多智能体意见冲突时的协作模式。借鉴 Multi-Agent Debate（Du et al., 2023）、Chatbot Arena（LMSYS）、Self-Consistency（Wang et al., 2022）、Society of Mind（Minsky）、ReConcile（Chen et al., 2023）、Multi-Agent Collaboration via Debate。
>
> **目的**：当多个智能体对同一问题给出不同/冲突的判断时（如 cross-border-specialist 和 legal-advisor 对跨境继承适用法律意见不一），通过结构化辩论 + 投票机制收敛到更可靠的结论，而非简单取"第一个回答"。

## 为什么需要辩论/投票

### 当前痛点

```
用户：我爸在加州去世，有中美两国房产，继承适用哪国法律？

路径 A：death-aftercare → 转介 cross-border-specialist
       cross-border-specialist 说：适用中国法律（被继承人本国法）

路径 B：death-aftercare → 转介 legal-advisor
       legal-advisor 说：适用不动产所在地法（加州房产适用加州法）

两个智能体意见冲突，用户无所适从
```

### 辩论/投票补强

```
1. 冲突检测：识别多个智能体回答的实质分歧
2. 结构化辩论：每个智能体陈述理由 + 引用法规
3. 交叉质询：互相质疑对方的论据
4. 投票收敛：多数/加权/共识投票
5. 仲裁机制：若投票不分胜负，由"仲裁 agent"裁决
```

## 适用场景

| 场景 | 是否启用辩论 | 原因 |
|------|------------|------|
| 单一智能体能答的常规问题 | 否 | 无冲突，无需辩论 |
| 跨域复杂问题（涉及 2+ 智能体专业） | 是 | 可能存在视角差异 |
| 法律适用冲突（多国法律） | 是 | 典型冲突场景 |
| 风险等级判定分歧（R2 vs R3） | 是 | 安全优先，不能错 |
| 转介目标不确定（A 还是 B？） | 否 | 走 router 路由即可 |
| 事实性查询（电话号码） | 否 | 有标准答案 |
| 政策解读差异 | 是 | 政策可能有多种解读 |

## 辩论流程

```python
# agents/debate_engine.py（伪代码）

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class DebateState(str, Enum):
    INITIATED = "initiated"
    OPENING = "opening"            # 各方陈述
    REBUTTAL = "rebuttal"          # 交叉质询
    CLOSING = "closing"            # 总结陈词
    VOTING = "voting"              # 投票
    ARBITRATION = "arbitration"    # 仲裁（若投票不分胜负）
    CONCLUDED = "concluded"        # 结束


@dataclass
class DebatePosition:
    """辩论立场"""
    agent_id: str
    position: str              # 该 agent 的主张
    supporting_evidence: list[dict]  # 支持证据
    # [{"type": "statute", "content": "民法典第1123条", "source": "..."}]
    confidence: float          # 0.0-1.0
    jurisdiction_basis: Optional[str]  # 法律依据/政策依据


@dataclass
class Debate:
    """辩论会话"""
    debate_id: str
    topic: str                 # 辩论主题
    participants: list[str]    # 参与辩论的 agent_id 列表
    positions: list[DebatePosition]  # 各方立场
    rounds: list[dict] = field(default_factory=list)  # 辩论轮次
    votes: dict[str, str] = field(default_factory=dict)  # {voter_id: voted_for_agent}
    final_resolution: Optional[str] = None
    state: DebateState = DebateState.INITIATED


class DebateEngine:
    """辩论引擎"""

    MAX_ROUNDS = 3  # 最多 3 轮（opening + rebuttal + closing）

    def should_debate(self, responses: list[dict]) -> bool:
        """判断是否需要辩论 - 检测冲突"""
        if len(responses) < 2:
            return False

        # 1. 提取各回答的核心主张
        positions = [self._extract_position(r) for r in responses]

        # 2. 用 LLM 判断是否存在实质冲突
        conflict_check = call_llm(f"""
        判断以下多个回答是否存在实质冲突：

        回答 1（{responses[0]['agent']}）：{responses[0]['response']}
        回答 2（{responses[1]['agent']}）：{responses[1]['response']}
        ...

        实质冲突 = 对同一问题给出不同结论（不只是表述差异）。
        输出：{{"conflict": true/false, "conflict_type": "...", "description": "..."}}
        """)
        return conflict_check["conflict"]

    async def initiate_debate(self, topic: str, participants: list[str],
                              initial_responses: list[dict]) -> Debate:
        """发起辩论"""
        debate = Debate(
            debate_id=str(uuid4()),
            topic=topic,
            participants=participants,
            positions=[
                DebatePosition(
                    agent_id=r["agent"],
                    position=self._extract_position(r),
                    supporting_evidence=r.get("evidence", []),
                    confidence=r.get("confidence", 0.7),
                )
                for r in initial_responses
            ],
        )

        # Round 1: Opening（各方陈述）
        await self._opening_round(debate)

        # Round 2: Rebuttal（交叉质询）
        await self._rebuttal_round(debate)

        # Round 3: Closing（总结）
        await self._closing_round(debate)

        # 投票
        await self._voting(debate)

        # 若投票不分胜负，仲裁
        if debate.state == DebateState.ARBITRATION:
            await self._arbitrate(debate)

        debate.state = DebateState.CONCLUDED
        return debate

    async def _opening_round(self, debate: Debate):
        """Round 1：各方陈述立场和证据"""
        for position in debate.positions:
            statement = await self._agent_speak(
                agent_id=position.agent_id,
                prompt=f"""
                辩论主题：{debate.topic}

                你的立场：{position.position}
                你的证据：{position.supporting_evidence}

                请陈述你的立场和理由。要求：
                1. 明确你的结论
                2. 引用具体的法规/政策条文（必须标注来源）
                3. 说明你的论证逻辑
                4. 不得编造，无证据的论点必须标注"无确切依据"

                诚信约束（integrity-framework）：
                - 不编造法条
                - 不编造案例
                - 不确定的部分必须标注置信度
                """,
            )
            debate.rounds.append({
                "round": 1,
                "agent": position.agent_id,
                "type": "opening",
                "statement": statement,
            })

    async def _rebuttal_round(self, debate: Debate):
        """Round 2：交叉质询"""
        for position in debate.positions:
            # 把其他 agent 的 opening 给这个 agent，让它反驳
            others_opening = [r for r in debate.rounds
                              if r["round"] == 1 and r["agent"] != position.agent_id]

            rebuttal = await self._agent_speak(
                agent_id=position.agent_id,
                prompt=f"""
                辩论主题：{debate.topic}
                你的立场：{position.position}

                其他参与方的陈述：
                {others_opening}

                请反驳其他参与方的论点。要求：
                1. 针对具体论点反驳，不要泛泛而谈
                2. 指出对方论证的漏洞（如：法条引用错误、逻辑跳跃、忽略了某条法规）
                3. 若对方论点合理，可以部分承认
                4. 不得编造证据
                """,
            )
            debate.rounds.append({
                "round": 2,
                "agent": position.agent_id,
                "type": "rebuttal",
                "statement": rebuttal,
            })

    async def _closing_round(self, debate: Debate):
        """Round 3：总结陈词"""
        for position in debate.positions:
            closing = await self._agent_speak(
                agent_id=position.agent_id,
                prompt=f"""
                辩论主题：{debate.topic}
                你的初始立场：{position.position}
                辩论过程：{debate.rounds}

                请总结你的最终立场。你可以：
                1. 坚持原立场（说明为什么对方的反驳不成立）
                2. 修正立场（说明对方哪个论点说服了你）
                3. 提出折中方案

                输出 JSON：
                {{
                  "final_position": "...",
                  "confidence": 0.0-1.0,
                  "concessions_made": ["..."],
                  "key_evidence": ["..."]
                }}
                """,
            )
            parsed = parse_json(closing)
            position.position = parsed["final_position"]
            position.confidence = parsed["confidence"]
            debate.rounds.append({
                "round": 3,
                "agent": position.agent_id,
                "type": "closing",
                "statement": closing,
            })

    async def _voting(self, debate: Debate):
        """投票"""
        # 投票人：不参与辩论的其他智能体 + 一个中立的"评审 agent"
        voters = [a for a in ALL_AGENTS if a not in debate.participants]
        voters.append("debate-arbiter")  # 仲裁 agent

        for voter in voters:
            vote = await self._agent_speak(
                agent_id=voter,
                prompt=f"""
                辩论主题：{debate.topic}
                辩论过程：{debate.rounds}
                各方最终立场：{[(p.agent_id, p.position, p.confidence) for p in debate.positions]}

                作为中立的评审，请投票支持你认为最合理的立场。
                评判标准：
                1. 论据是否扎实（法规引用是否准确）
                2. 逻辑是否严密
                3. 是否承认了对方的合理论点
                4. 证据是否标注来源

                输出 JSON：
                {{
                  "vote_for": "agent_id",
                  "reason": "...",
                  "confidence": 0.0-1.0
                }}
                """,
            )
            parsed = parse_json(vote)
            debate.votes[voter] = parsed["vote_for"]

        # 统计票数
        vote_counts = {}
        for voted_for in debate.votes.values():
            vote_counts[voted_for] = vote_counts.get(voted_for, 0) + 1

        # 判断是否有明确胜出
        max_votes = max(vote_counts.values())
        winners = [a for a, v in vote_counts.items() if v == max_votes]

        if len(winners) == 1:
            # 明确胜出
            winner = winners[0]
            winning_position = next(p for p in debate.positions if p.agent_id == winner)
            debate.final_resolution = {
                "winner": winner,
                "position": winning_position.position,
                "vote_counts": vote_counts,
                "confidence": winning_position.confidence,
            }
        else:
            # 平票，进入仲裁
            debate.state = DebateState.ARBITRATION

    async def _arbitrate(self, debate: Debate):
        """仲裁 - 由 arbiter agent 给出最终裁决"""
        arbitration = await self._agent_speak(
            agent_id="debate-arbiter",
            prompt=f"""
            辩论主题：{debate.topic}
            辩论过程：{debate.rounds}
            投票结果：{debate.votes}（平票）

            作为仲裁者，请给出最终裁决。要求：
            1. 综合各方论点
            2. 指出关键的分歧点
            3. 给出最可能正确的结论（标注置信度）
            4. 若无法确定，明确说"需要专业律师/领事确认"
            5. 不得编造证据

            输出 JSON：
            {{
              "resolution": "...",
              "confidence": 0.0-1.0,
              "key_disagreement": "...",
              "professional_referral_needed": true/false,
              "referral_target": "lawyer/consul/notary/..."
            }}
            """,
        )
        parsed = parse_json(arbitration)
        debate.final_resolution = parsed
```

## 投票策略

```python
# agents/voting_strategies.py（伪代码）

class VotingStrategy:
    """投票策略基类"""
    def vote(self, positions: list[DebatePosition], voters: list[str]) -> dict:
        raise NotImplementedError


class MajorityVote(VotingStrategy):
    """多数投票 - 简单多数胜出"""

    def vote(self, positions, voters):
        votes = {}
        for voter in voters:
            # 每个 voter 选一个 position
            chosen = self._choose(positions, voter)
            votes[chosen.agent_id] = votes.get(chosen.agent_id, 0) + 1

        winner = max(votes, key=votes.get)
        return {"winner": winner, "votes": votes, "strategy": "majority"}


class WeightedVote(VotingStrategy):
    """加权投票 - 不同 voter 权重不同"""

    VOTER_WEIGHTS = {
        "legal-advisor": 1.2,        # 法律问题权重高
        "cross-border-specialist": 1.2,  # 跨境问题权重高
        "financial-analyst": 1.0,
        "policy-researcher": 1.1,    # 政策问题权重略高
        "death-aftercare": 0.9,      # 通用 agent 权重略低
        "medical-guide": 0.9,
        "debate-arbiter": 1.5,       # 仲裁者权重最高
    }

    def vote(self, positions, voters):
        weighted_votes = {}
        for voter in voters:
            chosen = self._choose(positions, voter)
            weight = self.VOTER_WEIGHTS.get(voter, 1.0)
            weighted_votes[chosen.agent_id] = weighted_votes.get(chosen.agent_id, 0) + weight

        winner = max(weighted_votes, key=weighted_votes.get)
        return {"winner": winner, "weighted_votes": weighted_votes, "strategy": "weighted"}


class ConfidenceWeightedVote(VotingStrategy):
    """置信度加权投票 - 把 position 自身的 confidence 也纳入"""

    def vote(self, positions, voters):
        scores = {}
        for voter in voters:
            chosen = self._choose(positions, voter)
            # 分数 = voter 权重 × position 自身置信度
            voter_weight = WeightedVote.VOTER_WEIGHTS.get(voter, 1.0)
            score = voter_weight * chosen.confidence
            scores[chosen.agent_id] = scores.get(chosen.agent_id, 0) + score

        winner = max(scores, key=scores.get)
        return {"winner": winner, "scores": scores, "strategy": "confidence_weighted"}


class ConsensusVote(VotingStrategy):
    """共识投票 - 要求 2/3 多数才算胜出，否则进入仲裁"""

    THRESHOLD = 0.67

    def vote(self, positions, voters):
        votes = {}
        for voter in voters:
            chosen = self._choose(positions, voter)
            votes[chosen.agent_id] = votes.get(chosen.agent_id, 0) + 1

        total = len(voters)
        max_votes = max(votes.values())
        if max_votes / total >= self.THRESHOLD:
            winner = max(votes, key=votes.get)
            return {"winner": winner, "votes": votes, "strategy": "consensus",
                    "consensus_reached": True}
        else:
            return {"votes": votes, "strategy": "consensus",
                    "consensus_reached": False, "needs_arbitration": True}
```

## 与现有架构的集成

```python
# agents/debate_integration.py

class DebateTrigger:
    """检测何时触发辩论"""

    def check_after_transfer(self, state: ConversationState) -> Optional[Debate]:
        """
        在转介后检测：若用户同时被转介到多个智能体，
        且他们的回答冲突，触发辩论。
        """
        # 1. 检查是否有多个智能体都给出了回答
        responses = state.get("multi_agent_responses", [])
        if len(responses) < 2:
            return None

        # 2. 检测冲突
        engine = DebateEngine()
        if engine.should_debate(responses):
            # 3. 发起辩论
            return engine.initiate_debate(
                topic=state["user_input"],
                participants=[r["agent"] for r in responses],
                initial_responses=responses,
            )
        return None


class DebateArbiter:
    """仲裁 agent - 专门用于辩论仲裁"""

    AGENT_CARD = {
        "agent_id": "debate-arbiter",
        "name": "辩论仲裁员",
        "description": "中立的仲裁者，在多智能体意见冲突时给出最终裁决。不偏袒任何一方，只依据证据和逻辑判断。",
        "capabilities": ["debate-arbitration", "conflict-resolution"],
        "rules_loaded": ["integrity-framework.md", "conflict-resolution.md"],
    }

    async def arbitrate(self, debate: Debate) -> dict:
        """仲裁"""
        # arbiter 不参与辩论，只在投票平票时介入
        # 见 DebateEngine._arbitrate
        pass
```

## 辩论的可观测性

```python
# agents/debate_observability.py

def trace_debate(debate: Debate):
    """把辩论过程记录为 OTel trace"""
    # 1. 一个 root span（debate session）
    with tracer.start_as_current_span("debate.session") as root:
        root.set_attribute("debate.id", debate.debate_id)
        root.set_attribute("debate.topic", debate.topic)
        root.set_attribute("debate.participants", debate.participants)

        # 2. 每个 round 一个子 span
        for round_data in debate.rounds:
            with tracer.start_as_current_span(f"debate.round.{round_data['round']}") as round_span:
                round_span.set_attribute("round.type", round_data["type"])
                round_span.set_attribute("round.agent", round_data["agent"])
                round_span.set_attribute("round.statement", round_data["statement"][:200])

        # 3. 投票 span
        with tracer.start_as_current_span("debate.voting") as vote_span:
            vote_span.set_attribute("votes", debate.votes)
            vote_span.set_attribute("resolution", debate.final_resolution)
```

## 诚信约束（关键）

辩论过程中必须严格遵守 integrity-framework：

```python
DEBATE_INTEGRITY_RULES = """
辩论参与方必须遵守：
1. 不编造法条/政策条文（每条引用必须标注来源）
2. 不编造案例
3. 不确定的部分必须标注置信度（高/中/低）
4. 若对方指出自己的证据有误，必须承认
5. 不得使用"大概""可能""应该"等模糊词替代证据
6. 投票时必须说明理由（不能"凭感觉"投票）

仲裁者额外约束：
1. 不得编造证据支持某一方
2. 若证据不足以裁决，必须说"需要专业人士确认"
3. 不得偏向任何一方（即使该方是本平台的 agent）
"""
```

## 评估指标

| 指标 | 目标 | 说明 |
|------|------|------|
| 冲突检测准确率 | ≥ 0.90 | 正确识别实质冲突 |
| 辩论收敛率 | ≥ 0.85 | 辩论后达成共识（不需仲裁） |
| 仲裁准确率 | ≥ 0.90 | 仲裁结论与专家判断一致 |
| 辩论平均轮次 | ≤ 3 | 不过度辩论 |
| 辩论平均延迟 | ≤ 30s | 可接受范围 |
| 诚信违规率 | 0.0 | 辩论中无编造 |
| 用户满意度 | ≥ 0.80 | 辩论结果对用户有用 |

## 版本

- v1.0 初始辩论/投票方案（3 轮辩论 + 4 种投票策略 + 仲裁机制 + 诚信约束 + 可观测性）
```
