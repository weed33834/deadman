"""RAGAS 集成 - RAG 评估指标（faithfulness / context_precision / answer_relevancy）

作为 ThreeLayerEvaluator 之外的可选评估层，专门评估 RAG 检索质量。
RAGAS 为可选依赖，不可用时跳过。

用法：
    from .ragas_evaluator import RAGASEvaluator
    evaluator = RAGASEvaluator()
    result = await evaluator.evaluate(
        question="...",
        answer="...",
        contexts=["...", "..."],
    )
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# RAGAS 可选依赖
try:
    from ragas import evaluate as ragas_evaluate  # type: ignore
    from ragas.metrics import (  # type: ignore
        answer_relevancy,
        context_precision,
        faithfulness,
    )
    from datasets import Dataset  # type: ignore

    _RAGAS_AVAILABLE = True
except ImportError:
    ragas_evaluate = None  # type: ignore
    _RAGAS_AVAILABLE = False


class RAGASEvaluator:
    """RAGAS RAG 评估器

    评估 RAG 检索质量的三个核心指标：
    - faithfulness: 答案是否忠于检索到的上下文（无幻觉）
    - context_precision: 检索的上下文是否精确（相关内容排名靠前）
    - answer_relevancy: 答案是否与问题相关

    RAGAS 不可用时返回 degraded=true。
    """

    def __init__(self) -> None:
        self.available = _RAGAS_AVAILABLE

    async def evaluate(
        self,
        question: str,
        answer: str,
        contexts: list[str],
        ground_truth: str | None = None,
    ) -> dict[str, Any]:
        """执行 RAGAS 评估

        Args:
            question: 用户问题
            answer: 智能体回答
            contexts: RAG 检索到的上下文列表
            ground_truth: 可选的标准答案

        Returns:
            {
                "available": bool,
                "faithfulness": float,    # 0-1，越高越好
                "context_precision": float,
                "answer_relevancy": float,
                "degraded": bool,         # RAGAS 不可用时为 true
            }
        """
        if not self.available:
            return {
                "available": False,
                "degraded": True,
                "note": "ragas 包未安装，pip install ragas 后可获得 RAG 评估能力",
            }

        try:
            # 构造 RAGAS 数据集
            data = {
                "question": [question],
                "answer": [answer],
                "contexts": [contexts],
            }
            if ground_truth:
                data["ground_truth"] = [ground_truth]

            dataset = Dataset.from_dict(data)

            # 执行评估
            metrics = [faithfulness, context_precision, answer_relevancy]
            result = ragas_evaluate(dataset, metrics=metrics)

            return {
                "available": True,
                "degraded": False,
                "faithfulness": float(result.get("faithfulness", 0)),
                "context_precision": float(result.get("context_precision", 0)),
                "answer_relevancy": float(result.get("answer_relevancy", 0)),
            }
        except Exception as exc:
            logger.warning("RAGAS 评估失败: %s", exc)
            return {
                "available": True,
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def evaluate_sync(
        self,
        question: str,
        answer: str,
        contexts: list[str],
        ground_truth: str | None = None,
    ) -> dict[str, Any]:
        """同步版本（RAGAS 本身是同步的）"""
        if not self.available:
            return {
                "available": False,
                "degraded": True,
                "note": "ragas 包未安装",
            }

        try:
            data = {
                "question": [question],
                "answer": [answer],
                "contexts": [contexts],
            }
            if ground_truth:
                data["ground_truth"] = [ground_truth]

            dataset = Dataset.from_dict(data)
            metrics = [faithfulness, context_precision, answer_relevancy]
            result = ragas_evaluate(dataset, metrics=metrics)

            return {
                "available": True,
                "degraded": False,
                "faithfulness": float(result.get("faithfulness", 0)),
                "context_precision": float(result.get("context_precision", 0)),
                "answer_relevancy": float(result.get("answer_relevancy", 0)),
            }
        except Exception as exc:
            return {
                "available": True,
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
            }


# 全局单例
ragas_evaluator = RAGASEvaluator()
