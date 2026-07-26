"""Tree of Thought (ToT) - 多路径推理 + 评估 + 剪枝

P1.2 实现（v1.2 计划文档 P1.2）：对复杂决策生成 N=3 条候选推理路径，每路径
独立 LLM 调用，evaluator 打分选最优；每个推理节点用独立 LLM 做 fact-check
（借鉴 SelfCheckGPT），失败节点剪枝。

核心数据结构：
- ThoughtNode: 树节点 {node_id, thought, evaluation_score, parent_id, children, fact_check_passed, pruned}
- ToTResult: 求解结果 {best_thought, best_score, best_node_id, nodes, degraded, note}

设计要点：
- generate_paths(query, n=3): 生成 N 条候选路径（独立 LLM 调用，温度递增）
- evaluate(node): LLM 给节点打分（0-1）
- fact_check(node): 独立 LLM 验证事实性，失败剪枝
- solve(query): 生成 → 评估 → fact-check → 选最优

韧性 / 安全特性（三大铁律）：
- feature flag: DEADMAN_TOT_ENABLED=0（默认关闭，触发由外部调用方决定）
- LLM 不可用 → 返回降级 ToTResult
- 评估失败 → 默认 score=0.5
- fact_check 失败 → 默认通过（不误剪枝）
- 所有路径被剪枝 → 返回最高分（即便被剪枝），并记 note

降级路径：
- LLM 不可用 → ToTResult(degraded=True)
- 无候选路径生成 → ToTResult(degraded=True)
- evaluate/fact_check 异常 → 默认值，不阻断
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient

logger = logging.getLogger(__name__)

# =====================================================================
# 配置（全部 feature flag，默认安全）
# =====================================================================

# ToT 总开关：默认关闭（触发由外部调用方决定，避免成本爆炸）
TOT_ENABLED: bool = os.environ.get("DEADMAN_TOT_ENABLED", "0").lower() in (
    "1", "true", "yes", "on",
)

# 默认路径数
TOT_DEFAULT_PATHS: int = int(os.environ.get("DEADMAN_TOT_DEFAULT_PATHS", "3"))

# fact-check 失败阈值（score < 此值剪枝）— 当前实现用 passed=False 直接剪枝
TOT_FACT_CHECK_THRESHOLD: float = float(
    os.environ.get("DEADMAN_TOT_FACT_CHECK_THRESHOLD", "0.5")
)


# =====================================================================
# 数据结构
# =====================================================================


@dataclass
class ThoughtNode:
    """ToT 树节点"""

    node_id: str
    thought: str = ""
    evaluation_score: float = 0.0
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)  # child node_ids
    fact_check_passed: bool | None = None  # None=未检查
    pruned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "thought": self.thought,
            "evaluation_score": self.evaluation_score,
            "parent_id": self.parent_id,
            "children": list(self.children),
            "fact_check_passed": self.fact_check_passed,
            "pruned": self.pruned,
        }


@dataclass
class ToTResult:
    """ToT 搜索结果"""

    best_thought: str = ""
    best_score: float = 0.0
    best_node_id: str = ""
    nodes: list[ThoughtNode] = field(default_factory=list)
    degraded: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_thought": self.best_thought,
            "best_score": self.best_score,
            "best_node_id": self.best_node_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "degraded": self.degraded,
            "note": self.note,
        }


# =====================================================================
# Prompt 模板
# =====================================================================


_GENERATE_PROMPT = """你是身后事平台推理智能体。请对以下问题给出一条独立推理路径。

问题: {query}

要求:
- 给出完整推理过程（不要列其他路径，只给这一条）
- 推理结束给出最终结论
- 输出格式：纯文本，包含推理过程与结论"""

_EVALUATE_PROMPT = """评估以下推理的质量。

问题: {query}
推理: {thought}

评估维度:
- 事实准确性 (0-1)
- 逻辑一致性 (0-1)
- 完整性 (0-1)

输出 JSON: {{"score": 0.0-1.0, "reason": "简短说明"}}"""

_FACT_CHECK_PROMPT = """判断以下推理中的事实陈述是否正确。

问题: {query}
推理: {thought}

只输出 JSON: {{"passed": true|false, "issues": "问题说明(若有)"}}"""


# =====================================================================
# TreeOfThought
# =====================================================================


class TreeOfThought:
    """Tree of Thought 推理框架

    用法：
        tot = TreeOfThought(llm=strong_llm)
        result = await tot.solve(query)
        if not result.degraded:
            answer = result.best_thought
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        default_paths: int = TOT_DEFAULT_PATHS,
    ) -> None:
        self.llm = llm
        self.default_paths = max(1, default_paths)
        self._nodes: dict[str, ThoughtNode] = {}

    async def generate_paths(
        self, query: str, n: int = 3
    ) -> list[ThoughtNode]:
        """生成 N 条候选推理路径（独立 LLM 调用，温度递增增多样性）

        Args:
            query: 用户问题
            n: 路径数

        Returns:
            生成的 ThoughtNode 列表（LLM 不可用时返回空列表）
        """
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return []
        n = max(1, n)
        prompt = _GENERATE_PROMPT.format(query=query)
        messages = [{"role": "user", "content": prompt}]
        nodes: list[ThoughtNode] = []
        # 顺序调用（避免 mock LLM 并发问题，也省 token）
        for i in range(n):
            try:
                # 温度随采样序号微调（增多样性）
                thought = await self.llm.chat(messages, temperature=0.7 + i * 0.05)
            except Exception as e:
                logger.warning("ToT 生成路径 %d 失败: %s", i, e)
                thought = ""
            node = ThoughtNode(
                node_id=f"n{uuid.uuid4().hex[:8]}",
                thought=thought,
                parent_id=None,
            )
            self._nodes[node.node_id] = node
            nodes.append(node)
        return nodes

    async def evaluate(self, node: ThoughtNode, query: str = "") -> float:
        """LLM 给节点打分（0-1）

        Args:
            node: 待评估节点（评估结果写回 node.evaluation_score）
            query: 原始问题（提供上下文给评估器）

        Returns:
            分数 0.0-1.0（评估失败默认 0.5）
        """
        if not self.llm or not getattr(self.llm, "api_key", ""):
            node.evaluation_score = 0.5
            return 0.5
        prompt = _EVALUATE_PROMPT.format(query=query, thought=node.thought)
        try:
            data = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            score = float(data.get("score", 0.5))
            # 钳制到 [0,1]
            score = max(0.0, min(1.0, score))
            node.evaluation_score = score
            return score
        except Exception as e:
            logger.warning("ToT 评估失败: %s", e)
            node.evaluation_score = 0.5
            return 0.5

    async def fact_check(self, node: ThoughtNode, query: str = "") -> bool:
        """独立 LLM 验证事实性，失败剪枝

        Args:
            node: 待检查节点（结果写回 node.fact_check_passed，失败则 node.pruned=True）
            query: 原始问题

        Returns:
            True 表示通过；False 表示失败（节点已剪枝）
        """
        if not self.llm or not getattr(self.llm, "api_key", ""):
            # LLM 不可用 → 默认通过，不误剪枝
            node.fact_check_passed = True
            return True
        prompt = _FACT_CHECK_PROMPT.format(query=query, thought=node.thought)
        try:
            data = await self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            passed = bool(data.get("passed", True))
            node.fact_check_passed = passed
            if not passed:
                node.pruned = True
            return passed
        except Exception as e:
            # fact_check 异常 → 默认通过（不误剪枝）
            logger.warning("ToT fact_check 失败，默认通过: %s", e)
            node.fact_check_passed = True
            return True

    async def solve(self, query: str, n: int | None = None) -> ToTResult:
        """完整 ToT 求解：生成 → 评估 → fact-check → 选最优

        Args:
            query: 用户问题
            n: 路径数（None 用 default_paths）

        Returns:
            ToTResult，degraded=True 表示 LLM 不可用或无候选路径
        """
        result = ToTResult()
        if not self.llm or not getattr(self.llm, "api_key", ""):
            result.degraded = True
            result.note = "LLM 未配置，ToT 降级"
            return result

        n_paths = n or self.default_paths
        # 1. 生成 N 条候选路径
        nodes = await self.generate_paths(query, n=n_paths)
        if not nodes:
            result.degraded = True
            result.note = "无候选路径生成"
            return result

        # 2. 评估 + fact_check 每个节点
        for node in nodes:
            await self.evaluate(node, query=query)
            await self.fact_check(node, query=query)

        # 3. 选最优：fact_check 通过的最高分节点
        candidates = [
            n for n in nodes
            if not n.pruned and n.fact_check_passed is not False
        ]
        if not candidates:
            # 全部剪枝 → 返回最高分（即便被剪枝）
            candidates = nodes
            result.note = "所有路径 fact_check 失败，返回最高分"

        best = max(candidates, key=lambda n: n.evaluation_score, default=None)
        if best is None:
            result.degraded = True
            result.note = "无可用节点"
            return result

        result.best_node_id = best.node_id
        result.best_thought = best.thought
        result.best_score = best.evaluation_score
        result.nodes = list(nodes)
        return result
