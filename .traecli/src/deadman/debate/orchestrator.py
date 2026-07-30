"""DebateOrchestrator - 多智能体辩论编排器。

对应 agents/Debate-Voting.md 的伪代码实现，做最小必要 + 工程化扩展：

核心流程：
1. should_debate：检测多个 agent 回答是否存在实质冲突（LLM 判定）
2. _opening_round：各方陈述立场 + 引用法规/政策
3. _rebuttal_round：交叉质询，反驳对方论点
4. _closing_round：总结陈词，可修正立场
5. _voting：未参与辩论的 agent + arbiter 投票
6. _arbitrate：平票时 arbiter 给最终裁决

工程化扩展（计划文档 P0.1 中级/高级档）：
- 收敛检测：连续 2 轮 position 无变化 → 提前结束
- token 预算：累计超 MAX_TOKEN_BUDGET → 跳过剩余轮次直入仲裁
- 并发优化：同一 round 内多个 agent 陈述用 asyncio.gather 并行
- 诚信约束：integrity-framework prompt 强制注入
- 仲裁降级：confidence < 0.6 → 自动追加"需要专业律师/领事确认"
- trace span：root.session → round.opening/rebuttal/closing → voting/arbitration

公开接口：
- DebateOrchestrator(llm_client, voting_strategy).run_debate(topic, participants, initial_responses)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

from ..llm import LLMClient
from ..llm import llm_client as default_llm_client
from ..observability.tracer import SpanType, tracer
from .models import Debate, DebatePosition, DebateState
from .voting import VotingStrategy, get_strategy

logger = logging.getLogger(__name__)


# === 6 个并列智能体名（与 orchestration/nodes.py AGENT_NAMES 一致，下划线版）===
# 用于收集"非参与方"作为投票人
_ALL_AGENTS: list[str] = [
    "death_aftercare",
    "legal_advisor",
    "financial_analyst",
    "policy_researcher",
    "cross_border_specialist",
    "medical_guide",
]

# 仲裁者 ID（不参与辩论，仅投票/仲裁）
_ARBITER_ID = "debate_arbiter"

# 预算与终止硬上限（防止无限辩论与 token 爆炸）
MAX_ROUNDS = 3  # opening + rebuttal + closing
MAX_TOKEN_BUDGET = 8000  # 单次辩论总 token 上限
MIN_VOTERS = 2  # 投票人最少数量（含 arbiter），不足则强制仲裁


class DebateOrchestrator:
    """辩论编排器 - 串起 3 轮辩论 + 投票 + 可选仲裁。

    用法：
        orchestrator = DebateOrchestrator(llm_client=llm, voting_strategy="weighted")
        result = await orchestrator.run_debate(
            topic="跨境继承适用哪国法律？",
            participants=["cross_border_specialist", "legal_advisor"],
            initial_responses=[
                {"agent": "cross_border_specialist", "response": "适用中国法律..."},
                {"agent": "legal_advisor", "response": "适用不动产所在地法..."},
            ],
        )
        # result = {"debate_id", "rounds", "votes", "final_resolution", "arbitration_needed"}
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        voting_strategy: str = "weighted",
        max_rounds: int = MAX_ROUNDS,
        max_token_budget: int = MAX_TOKEN_BUDGET,
    ) -> None:
        self.llm = llm_client or default_llm_client
        self.voting_strategy_name = voting_strategy
        self.voting_strategy: VotingStrategy = get_strategy(voting_strategy)
        self.max_rounds = max_rounds
        self.max_token_budget = max_token_budget

    # =================================================================
    # 公开入口
    # =================================================================

    async def run_debate(
        self,
        topic: str,
        participants: list[str],
        initial_responses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """发起一次完整辩论。

        Args:
            topic: 辩论主题（用户原问题）
            participants: 参与 agent_id 列表（≥2）
            initial_responses: 各方初始回答 [{"agent", "response", "evidence"?, "confidence"?}]

        Returns:
            {"debate_id", "rounds", "votes", "final_resolution", "arbitration_needed", "topic", "participants"}
        """
        # === 进入 root trace span ===
        root_span_id = tracer.start_span(
            SpanType.DEBATE,
            f"debate.session.{uuid.uuid4().hex[:8]}",
            {
                "debate.topic": topic[:200],
                "debate.participants": ",".join(participants),
                "debate.voting_strategy": self.voting_strategy_name,
                "debate.max_rounds": self.max_rounds,
            },
        )

        # token 计数器清零
        self._current_debate_token = 0

        try:
            # 0. 初始化 debate
            debate = await self._init_debate(topic, participants, initial_responses)
            debate.state = DebateState.OPENING
            tracer.start_span(
                SpanType.DEBATE,
                "debate.opening",
                {"debate.round": 1, "debate.participants_count": len(participants)},
            )

            # 1. Round 1: Opening - 各方陈述（并行）
            await self._opening_round(debate)

            # 预算检查
            if debate.token_used > self.max_token_budget:
                logger.warning(
                    "辩论 token 预算超限 %d/%d，跳过 rebuttal 直入 voting",
                    debate.token_used,
                    self.max_token_budget,
                )
            else:
                # 2. Round 2: Rebuttal - 交叉质询（并行）
                debate.state = DebateState.REBUTTAL
                await self._rebuttal_round(debate)

                # 收敛检测：若两轮 position 无变化，提前结束
                if self._is_converged(debate):
                    logger.info("辩论收敛检测命中，跳过 closing 提前投票")
                elif debate.token_used <= self.max_token_budget:
                    # 3. Round 3: Closing - 总结陈词（并行）
                    debate.state = DebateState.CLOSING
                    await self._closing_round(debate)

            # 4. 投票
            debate.state = DebateState.VOTING
            await self._voting(debate)

            # 5. 平票则仲裁
            if debate.state == DebateState.ARBITRATION:
                await self._arbitrate(debate)

            debate.state = DebateState.CONCLUDED
            return self._format_result(debate)

        except Exception as exc:
            logger.exception("辩论执行异常: %s", exc)
            # 异常时返回部分结果 + error 字段，不抛
            return {
                "debate_id": str(uuid.uuid4()),
                "topic": topic,
                "participants": participants,
                "rounds": [],
                "votes": {},
                "final_resolution": None,
                "arbitration_needed": True,
                "error": f"{type(exc).__name__}: {exc}",
                "voting_strategy": self.voting_strategy_name,
            }
        finally:
            tracer.end_span(root_span_id, status="OK")

    # =================================================================
    # 阶段实现
    # =================================================================

    async def _init_debate(
        self,
        topic: str,
        participants: list[str],
        initial_responses: list[dict[str, Any]],
    ) -> Debate:
        """从初始回答构造 Debate 聚合根 + 提取各方立场。"""
        debate_id = str(uuid.uuid4())
        positions: list[DebatePosition] = []

        # 并发提取所有初始回答的核心立场
        tasks = [_extract_position(self.llm, r.get("response", "")) for r in initial_responses]
        position_texts = await asyncio.gather(*tasks, return_exceptions=True)

        for r, pos_text in zip(initial_responses, position_texts, strict=True):
            agent_id = r.get("agent", "")
            position_text = pos_text if isinstance(pos_text, str) else r.get("response", "")[:100]
            positions.append(
                DebatePosition(
                    agent_id=agent_id,
                    position=position_text,
                    supporting_evidence=r.get("evidence", []),
                    confidence=float(r.get("confidence", 0.7)),
                    jurisdiction_basis=r.get("jurisdiction_basis"),
                )
            )

        return Debate(
            debate_id=debate_id,
            topic=topic,
            participants=participants,
            positions=positions,
        )

    async def _opening_round(self, debate: Debate) -> None:
        """Round 1: 各方陈述立场 + 引用法规/政策（并行）"""
        tasks = [self._agent_opening(debate, pos) for pos in debate.positions]
        await asyncio.gather(*tasks, return_exceptions=True)
        # 不直接 await 防止一个失败导致全部失败

    async def _agent_opening(self, debate: Debate, position: DebatePosition) -> None:
        """单个 agent 的 opening 陈述"""
        prompt = self._build_opening_prompt(debate, position)
        statement = await self._safe_chat(prompt, temperature=0.3)
        debate.add_round(
            round_num=1,
            agent_id=position.agent_id,
            round_type="opening",
            statement=statement,
            extra={"position": position.position},
        )

    async def _rebuttal_round(self, debate: Debate) -> None:
        """Round 2: 交叉质询 - 每个 agent 反驳其他方的 opening"""
        # 收集所有 opening 陈词
        openings = [r for r in debate.rounds if r.get("type") == "opening"]
        tasks = [self._agent_rebuttal(debate, pos, openings) for pos in debate.positions]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _agent_rebuttal(
        self,
        debate: Debate,
        position: DebatePosition,
        openings: list[dict[str, Any]],
    ) -> None:
        """单个 agent 的 rebuttal"""
        others = [r for r in openings if r.get("agent") != position.agent_id]
        prompt = self._build_rebuttal_prompt(debate, position, others)
        statement = await self._safe_chat(prompt, temperature=0.4)
        debate.add_round(
            round_num=2,
            agent_id=position.agent_id,
            round_type="rebuttal",
            statement=statement,
        )

    async def _closing_round(self, debate: Debate) -> None:
        """Round 3: 总结陈词 - 各方可修正立场"""
        tasks = [self._agent_closing(debate, pos) for pos in debate.positions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 把 closing 中 LLM 输出的 final_position/confidence 更新回 position
        for pos, result in zip(debate.positions, results, strict=True):
            if isinstance(result, dict):
                if "final_position" in result:
                    pos.position = result["final_position"]
                if "confidence" in result:
                    with contextlib.suppress(TypeError, ValueError):
                        pos.confidence = float(result["confidence"])

    async def _agent_closing(self, debate: Debate, position: DebatePosition) -> dict[str, Any]:
        """单个 agent 的 closing - 输出 JSON {final_position, confidence, concessions_made, key_evidence}"""
        prompt = self._build_closing_prompt(debate, position)
        result = await self._safe_chat_json(prompt, temperature=0.3)
        # 兜底字段
        result.setdefault("final_position", position.position)
        result.setdefault("confidence", position.confidence)
        debate.add_round(
            round_num=3,
            agent_id=position.agent_id,
            round_type="closing",
            statement=json.dumps(result, ensure_ascii=False),
        )
        return result

    async def _voting(self, debate: Debate) -> None:
        """投票 - 未参与辩论的 agent + arbiter 投票"""
        # 投票人：非参与方的 6 个并列 agent + arbiter
        voters = [a for a in _ALL_AGENTS if a not in debate.participants]
        voters.append(_ARBITER_ID)

        # 投票人数不足 → 直接进仲裁
        if len(voters) < MIN_VOTERS:
            logger.warning("投票人不足 %d，强制仲裁", len(voters))
            debate.state = DebateState.ARBITRATION
            return

        # 并发投票
        tasks = [self._voter_vote(debate, voter) for voter in voters]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        vote_span = tracer.start_span(
            SpanType.DEBATE,
            "debate.voting",
            {"debate.voters_count": len(voters)},
        )
        try:
            for voter, result in zip(voters, results, strict=True):
                if isinstance(result, dict) and "vote_for" in result:
                    debate.votes[voter] = result["vote_for"]
        finally:
            tracer.end_span(vote_span, status="OK")

        # 用策略统计
        verdict = self.voting_strategy.vote(debate.positions, debate.votes)
        if verdict.get("needs_arbitration"):
            debate.state = DebateState.ARBITRATION
        else:
            winner = verdict["winner"]
            winning_pos = next((p for p in debate.positions if p.agent_id == winner), None)
            debate.final_resolution = {
                "winner": winner,
                "position": winning_pos.position if winning_pos else None,
                "vote_counts": verdict.get("votes")
                or verdict.get("weighted_votes")
                or verdict.get("scores"),
                "confidence": winning_pos.confidence if winning_pos else 0.5,
                "strategy": self.voting_strategy_name,
                "needs_professional_referral": winning_pos is None or winning_pos.confidence < 0.6,
            }

    async def _voter_vote(self, debate: Debate, voter_id: str) -> dict[str, Any]:
        """单个 voter 投票"""
        prompt = self._build_voting_prompt(debate, voter_id)
        return await self._safe_chat_json(prompt, temperature=0.2)

    async def _arbitrate(self, debate: Debate) -> None:
        """仲裁 - arbiter 给最终裁决"""
        arb_span = tracer.start_span(
            SpanType.DEBATE,
            "debate.arbitration",
            {"debate.arbiter": _ARBITER_ID},
        )
        try:
            prompt = self._build_arbitration_prompt(debate)
            result = await self._safe_chat_json(prompt, temperature=0.3)
            # 兜底字段
            result.setdefault("resolution", "无法确定，需专业人士确认")
            result.setdefault("confidence", 0.5)
            result.setdefault("key_disagreement", "")
            result.setdefault("professional_referral_needed", True)
            result.setdefault("referral_target", "lawyer")
            debate.final_resolution = result
        finally:
            tracer.end_span(arb_span, status="OK")

    # =================================================================
    # Prompt 构造（注入 integrity-framework 强约束）
    # =================================================================

    def _integrity_preamble(self) -> str:
        """诚信约束前置注入到所有辩论 prompt"""
        return """
【诚信约束 - 必须严格遵守 integrity-framework】
1. 不编造法条/政策条文（每条引用必须标注来源 URL 或文档名）
2. 不编造案例
3. 不确定的部分必须标注置信度（高/中/低）
4. 若对方指出自己的证据有误，必须承认
5. 不得使用"大概""可能""应该"等模糊词替代证据
6. 投票时必须说明理由（不能"凭感觉"投票）

【安全红线】
- 不得给出最终法律/医学诊断意见，仅做信息引导
- 涉及跨境/高风险（R3）问题必须标注"需要专业律师/领事确认"
"""

    def _build_opening_prompt(self, debate: Debate, position: DebatePosition) -> str:
        return f"""你是身后事多智能体平台的 {position.agent_id} 智能体，正在参与结构化辩论。

{self._integrity_preamble()}

## 辩论主题
{debate.topic}

## 你的初始立场
{position.position}

## 你的支持证据
{json.dumps(position.supporting_evidence, ensure_ascii=False) if position.supporting_evidence else "（暂无显式证据，请基于你已有的知识陈述）"}

## 任务
陈述你的立场和理由。要求：
1. 明确你的结论（一句话）
2. 引用具体的法规/政策条文（必须标注来源）
3. 说明你的论证逻辑（步骤清晰）
4. 无证据的论点必须标注"无确切依据"
5. 不超过 500 字

直接输出陈述内容，不要包含"作为 AI"等身份声明。
"""

    def _build_rebuttal_prompt(
        self,
        debate: Debate,
        position: DebatePosition,
        others_opening: list[dict[str, Any]],
    ) -> str:
        others_text = "\n\n".join(
            f"【{r.get('agent')} 的 opening】\n{r.get('statement', '')[:1000]}"
            for r in others_opening
        )
        return f"""你是身后事多智能体平台的 {position.agent_id} 智能体，正在参与辩论的第二轮：交叉质询。

{self._integrity_preamble()}

## 辩论主题
{debate.topic}

## 你的立场
{position.position}

## 其他参与方的 opening 陈述
{others_text}

## 任务
反驳其他参与方的论点。要求：
1. 针对具体论点反驳，不要泛泛而谈
2. 指出对方论证的漏洞（法条引用错误/逻辑跳跃/忽略某条法规）
3. 若对方论点合理，可以部分承认（诚信）
4. 不得编造证据
5. 不超过 500 字

直接输出反驳内容。
"""

    def _build_closing_prompt(self, debate: Debate, position: DebatePosition) -> str:
        rounds_summary = "\n".join(
            f"[Round {r.get('round')} - {r.get('type')}] {r.get('agent')}: {r.get('statement', '')[:300]}"
            for r in debate.rounds
        )
        return f"""你是身后事多智能体平台的 {position.agent_id} 智能体，正在参与辩论的第三轮：总结陈词。

{self._integrity_preamble()}

## 辩论主题
{debate.topic}

## 你的初始立场
{position.position}

## 辩论过程
{rounds_summary}

## 任务
总结你的最终立场。你可以：
1. 坚持原立场（说明为什么对方反驳不成立）
2. 修正立场（说明对方哪个论点说服了你）
3. 提出折中方案

输出 JSON（严格遵守，不要 markdown 代码块）：
{{
  "final_position": "你的最终立场（一句话）",
  "confidence": 0.0-1.0,
  "concessions_made": ["承认的对方论点"],
  "key_evidence": ["保留的关键证据"]
}}
"""

    def _build_voting_prompt(self, debate: Debate, voter_id: str) -> str:
        positions_text = "\n".join(
            f"- {p.agent_id}: {p.position}（confidence={p.confidence}）" for p in debate.positions
        )
        rounds_summary = "\n".join(
            f"[Round {r.get('round')} - {r.get('type')}] {r.get('agent')}: {r.get('statement', '')[:400]}"
            for r in debate.rounds
        )
        return f"""你是身后事多智能体平台的 {voter_id}，作为中立评审参与辩论投票。

{self._integrity_preamble()}

## 辩论主题
{debate.topic}

## 辩论过程
{rounds_summary}

## 各方最终立场
{positions_text}

## 任务
作为中立的评审，请投票支持你认为最合理的立场。评判标准：
1. 论据是否扎实（法规引用是否准确）
2. 逻辑是否严密
3. 是否承认了对方的合理论点
4. 证据是否标注来源

输出 JSON（严格遵守，不要 markdown 代码块）：
{{
  "vote_for": "agent_id（必须从参与方中选一个）",
  "reason": "投票理由（不超过 100 字）",
  "confidence": 0.0-1.0
}}
"""

    def _build_arbitration_prompt(self, debate: Debate) -> str:
        votes_text = json.dumps(debate.votes, ensure_ascii=False)
        rounds_summary = "\n".join(
            f"[Round {r.get('round')} - {r.get('type')}] {r.get('agent')}: {r.get('statement', '')[:400]}"
            for r in debate.rounds
        )
        return f"""你是身后事多智能体平台的辩论仲裁员（debate-arbiter），投票出现平票，需要你给出最终裁决。

{self._integrity_preamble()}

## 仲裁者额外约束
1. 不得编造证据支持某一方
2. 若证据不足以裁决，必须说"需要专业人士确认"
3. 不得偏向任何一方（即使该方是本平台的 agent）

## 辩论主题
{debate.topic}

## 辩论过程
{rounds_summary}

## 投票结果（平票）
{votes_text}

## 任务
给出最终裁决。输出 JSON（严格遵守，不要 markdown 代码块）：
{{
  "resolution": "综合裁决结论（不超过 200 字）",
  "confidence": 0.0-1.0,
  "key_disagreement": "关键分歧点",
  "professional_referral_needed": true/false,
  "referral_target": "lawyer/consul/notary/..."
}}
"""

    # =================================================================
    # LLM 调用安全封装（token 累加 + 异常降级）
    # =================================================================

    async def _safe_chat(self, prompt: str, temperature: float = 0.3) -> str:
        """安全调用 LLM chat - 异常时返回降级文本，不抛"""
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return "（LLM 不可用，跳过本轮陈述）"
        try:
            result = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            # 累加 token
            usage = getattr(self.llm, "last_usage", None) or {}
            self._accumulate_token(usage)
            return result or ""
        except Exception as exc:
            logger.warning("LLM chat 失败: %s", exc)
            return f"（陈述生成失败：{type(exc).__name__}）"

    async def _safe_chat_json(self, prompt: str, temperature: float = 0.2) -> dict[str, Any]:
        """安全调用 LLM chat_json - 异常时返回降级 dict，不抛"""
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return {}
        try:
            result = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            usage = getattr(self.llm, "last_usage", None) or {}
            self._accumulate_token(usage)
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            logger.warning("LLM chat_json 失败: %s", exc)
            return {}

    def _accumulate_token(self, usage: dict[str, Any]) -> None:
        """累加本次 LLM 调用的 token 到 budget（用于预算控制）"""
        if not usage:
            return
        # 优先用 total_tokens
        total = usage.get("total_tokens") or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        )
        with contextlib.suppress(TypeError, ValueError):
            self._current_debate_token += int(total)

    # =================================================================
    # 收敛检测 + 结果格式化
    # =================================================================

    def _is_converged(self, debate: Debate) -> bool:
        """收敛检测 - 若 rebuttal 阶段所有 agent 立场未变化则提前结束

        启发式：检查 rebuttal statement 是否包含"承认""同意""接受"等让步词
        """
        if not debate.rounds:
            return False
        rebuttals = [r for r in debate.rounds if r.get("type") == "rebuttal"]
        if len(rebuttals) < len(debate.positions):
            return False
        # 全部 rebuttal 都包含让步词 → 收敛
        concession_keywords = ["承认", "同意", "接受", "部分同意", "对方合理"]
        concession_count = 0
        for r in rebuttals:
            stmt = r.get("statement", "")
            if any(kw in stmt for kw in concession_keywords):
                concession_count += 1
        return concession_count >= len(debate.positions) * 0.5

    def _format_result(self, debate: Debate) -> dict[str, Any]:
        """格式化对外返回结果"""
        final_res = debate.final_resolution or {}
        arbitration_needed = debate.state == DebateState.ARBITRATION or (
            final_res.get("needs_professional_referral") is True
        )
        return {
            "debate_id": debate.debate_id,
            "topic": debate.topic,
            "participants": debate.participants,
            "rounds": debate.rounds,
            "votes": debate.votes,
            "final_resolution": final_res,
            "arbitration_needed": arbitration_needed,
            "state": debate.state.value,
            "token_used": self._current_debate_token,
            "voting_strategy": self.voting_strategy_name,
            "not_implemented": False,
        }

    @property
    def _current_debate_token(self) -> int:
        """当前 debate 的 token 累计（property 形式便于 _accumulate_token 访问）

        实际值挂在当前 debate 实例上 - 为简化实现，用一个实例属性。
        每次 run_debate 入口清零。
        """
        return getattr(self, "_token_counter", 0)

    @_current_debate_token.setter
    def _current_debate_token(self, value: int) -> None:
        self._token_counter = value  # type: ignore[attr-defined]


# =================================================================
# 模块级辅助：从初始回答提取核心立场
# =================================================================


async def _extract_position(llm: Any, response: str) -> str:
    """用 LLM 从初始回答提取一句话核心立场。

    失败时降级为截断（不超过 100 字）。
    """
    if not llm or not getattr(llm, "api_key", ""):
        return response[:100]
    try:
        prompt = (
            "从以下回答中提取核心立场（一句话结论，不超过 50 字）。"
            "只输出立场本身，不要前缀：\n\n"
            f"{response[:1000]}"
        )
        result = await llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return (result or response[:100]).strip()[:100]
    except Exception:
        return response[:100]
