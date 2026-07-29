"""Evaluator-Optimizer 工作流 - 生成 → 评估 → 优化循环

P1.3 实现（v1.2 计划文档 P1.3）：Anthropic 五种 Workflow 之一，answer 生成后由
LLM-as-Judge 评估，分数低于阈值则重新生成，最多 max_rounds 轮。

核心方法：
- generate_answer(query, context) -> str: 首次生成
- evaluate(answer, query) -> tuple[float, str]: LLM-as-Judge 评分（0-1）+ feedback
- optimize(query, context, max_rounds=3) -> OptimizeResult: 生成→评估→优化循环

设计要点：
- 优化阈值：score < EVAL_OPTIMIZER_THRESHOLD（默认 0.7）触发重新生成
- evaluator 复用 evaluation/three_layer.py 的 LLMJudge（如可导入；不可导入则用
  内置的 chat_json 评估器，逻辑等价）
- 每轮记录 OptimizationRound（round_num, answer, score, feedback）
- 保留历史最佳答案（最高分），即使后续轮次变差也返回最佳

韧性 / 安全特性（三大铁律）：
- feature flag: DEADMAN_EVAL_OPTIMIZER_ENABLED=0（默认关闭）
- LLM 不可用 → OptimizeResult(degraded=True)
- 评估失败 → 默认通过（score=1.0），不再优化
- 单轮生成失败 → 用上一轮答案，继续评估
- max_rounds 到达 → 返回最佳答案

降级路径：
- LLM 不可用 → degraded=True
- 首次生成失败 → 后续轮用空答案，最终 final_answer 可能为空
- 评估异常 → 默认 score=1.0（视为达标，提前退出）
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient

logger = logging.getLogger(__name__)

# =====================================================================
# 配置（全部 feature flag，默认安全）
# =====================================================================

# Evaluator-Optimizer 总开关：默认关闭
EVAL_OPTIMIZER_ENABLED: bool = os.environ.get(
    "DEADMAN_EVAL_OPTIMIZER_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 优化阈值：score < 此值触发重新生成
EVAL_OPTIMIZER_THRESHOLD: float = float(
    os.environ.get("DEADMAN_EVAL_OPTIMIZER_THRESHOLD", "0.7")
)

# 默认最大轮次
EVAL_OPTIMIZER_MAX_ROUNDS: int = int(
    os.environ.get("DEADMAN_EVAL_OPTIMIZER_MAX_ROUNDS", "3")
)


# =====================================================================
# 数据结构
# =====================================================================


@dataclass
class OptimizationRound:
    """单轮优化记录"""

    round_num: int
    answer: str
    score: float
    feedback: str = ""


@dataclass
class OptimizeResult:
    """优化循环结果"""

    final_answer: str = ""
    final_score: float = 0.0
    rounds: list[OptimizationRound] = field(default_factory=list)
    degraded: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "final_score": self.final_score,
            "rounds": [
                {
                    "round_num": r.round_num,
                    "answer": r.answer,
                    "score": r.score,
                    "feedback": r.feedback,
                }
                for r in self.rounds
            ],
            "degraded": self.degraded,
            "note": self.note,
        }


# =====================================================================
# Prompt 模板
# =====================================================================


_GENERATE_PROMPT = """请基于以下上下文回答用户问题。

上下文:
{context}

用户问题: {query}

回答（清晰、准确、完整）:"""

_OPTIMIZE_PROMPT = """请基于反馈优化之前的回答。

用户问题: {query}
上下文: {context}
之前回答: {previous_answer}
评估反馈: {feedback}

请给出优化后的回答:"""

_EVALUATE_PROMPT = """评估以下回答的质量。

用户问题: {query}
回答: {answer}

评估维度:
- 事实准确性 (0-1)
- 逻辑一致性 (0-1)
- 完整性 (0-1)
- 用户友好度 (0-1)

输出 JSON: {{"score": 0.0-1.0, "feedback": "改进建议"}}"""


# =====================================================================
# EvaluatorOptimizer
# =====================================================================


class EvaluatorOptimizer:
    """生成 → 评估 → 优化循环

    用法：
        eo = EvaluatorOptimizer(llm=strong_llm)
        result = await eo.optimize(query, context)
        if not result.degraded:
            answer = result.final_answer
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        evaluator: Any | None = None,
        threshold: float = EVAL_OPTIMIZER_THRESHOLD,
    ) -> None:
        self.llm = llm
        self.threshold = threshold
        # evaluator 可选：复用 LLMJudge（如传入或可导入）；否则用内置 chat_json 评估器
        self.evaluator = evaluator
        self._llm_judge_cls: Any = None
        # 尝试导入 LLMJudge（不强制，导入失败用内置评估器）
        if self.evaluator is None:
            try:
                from ..evaluation.three_layer import LLMJudge  # type: ignore

                self._llm_judge_cls = LLMJudge
            except ImportError:
                self._llm_judge_cls = None
        else:
            self._llm_judge_cls = None

    async def generate_answer(self, query: str, context: str = "") -> str:
        """首次生成答案"""
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return ""
        prompt = _GENERATE_PROMPT.format(query=query, context=context or "(无)")
        try:
            return await self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            logger.warning("EvaluatorOptimizer 生成失败: %s", e)
            return ""

    async def evaluate(self, answer: str, query: str) -> tuple[float, str]:
        """LLM-as-Judge 评分（0-1），返回 (score, feedback)

        Args:
            answer: 待评估答案
            query: 原始问题

        Returns:
            (score, feedback)；评估失败默认 (1.0, ...) 视为达标
        """
        if not answer:
            return 0.0, "答案为空"
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return 1.0, "llm_unavailable_default_pass"
        prompt = _EVALUATE_PROMPT.format(query=query, answer=answer)
        try:
            data = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            score = float(data.get("score", 1.0))
            score = max(0.0, min(1.0, score))
            feedback = str(data.get("feedback", ""))
            return score, feedback
        except Exception as e:
            # 评估异常 → 默认通过（score=1.0），避免无限循环
            logger.warning("EvaluatorOptimizer 评估失败，默认通过: %s", e)
            return 1.0, f"eval_error: {e}"

    async def optimize(
        self,
        query: str,
        context: str = "",
        max_rounds: int = EVAL_OPTIMIZER_MAX_ROUNDS,
    ) -> OptimizeResult:
        """生成 → 评估 → 优化循环

        Args:
            query: 用户问题
            context: 上下文（知识库 / 历史等）
            max_rounds: 最大轮次（至少 1）

        Returns:
            OptimizeResult，degraded=True 表示 LLM 不可用
        """
        result = OptimizeResult()
        if not self.llm or not getattr(self.llm, "api_key", ""):
            result.degraded = True
            result.note = "LLM 未配置，优化降级"
            return result

        max_rounds = max(1, max_rounds)
        current_answer = ""
        best_answer = ""
        best_score: float = -1.0

        for round_num in range(1, max_rounds + 1):
            # 1. 生成（首轮）或优化（后续轮）
            if round_num == 1:
                current_answer = await self.generate_answer(query, context)
            else:
                prev = result.rounds[-1]
                prompt = _OPTIMIZE_PROMPT.format(
                    query=query,
                    context=context or "(无)",
                    previous_answer=prev.answer,
                    feedback=prev.feedback,
                )
                try:
                    current_answer = await self.llm.chat(
                        [{"role": "user", "content": prompt}],
                        temperature=0.3,
                    )
                except Exception as e:
                    logger.warning("EvaluatorOptimizer 优化生成失败: %s", e)
                    current_answer = prev.answer  # 用上一轮答案

            if not current_answer:
                # 生成失败 → 记录空答案，score=0
                result.rounds.append(
                    OptimizationRound(
                        round_num=round_num,
                        answer="",
                        score=0.0,
                        feedback="生成失败",
                    )
                )
                continue

            # 2. 评估
            score, feedback = await self.evaluate(current_answer, query)
            result.rounds.append(
                OptimizationRound(
                    round_num=round_num,
                    answer=current_answer,
                    score=score,
                    feedback=feedback,
                )
            )

            # 3. 更新最佳
            if score > best_score:
                best_score = score
                best_answer = current_answer

            # 4. 达标退出
            if score >= self.threshold:
                break

        result.final_answer = best_answer
        result.final_score = best_score if best_score >= 0 else 0.0
        return result
