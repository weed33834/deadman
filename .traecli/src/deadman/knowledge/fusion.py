"""P8.3.5 多源知识融合(GraphitiRuntime + LightRAGRuntime + 向量库)。

设计目标:
    - 同时调用 GraphitiRuntime.search + LightRAGRuntime.search + 可选向量库
    - 按信任度(TrustScorer)加权融合多源结果
    - 检测冲突:不同来源对同一事实给出不同答案 → 标记 conflict
    - 置信度传播:加权后的最终置信度(0.0-1.0)

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 不修改子运行时状态(纯读)
    - 输出 FusionResult(answer + contributing_sources + confidence + conflicts)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from .graphiti_runtime import GraphitiRuntime, KGNode
from .lightrag_runtime import LightRAGRuntime, SearchResult
from .trust import TrustScorer

logger = logging.getLogger(__name__)


@dataclass
class ConflictItem:
    """单条冲突记录。

    Attributes:
        fact: 冲突的事实(简短描述)
        sources_with_values: 各来源对该事实给出的不同值
        resolution: 解决方式("highest_trust" / "majority" / "uncertain" / None)
    """

    fact: str
    sources_with_values: dict[str, str] = field(default_factory=dict)
    resolution: Optional[str] = None


@dataclass
class FusionResult:
    """多源融合结果。

    Attributes:
        answer: 最终答案(融合后的文本)
        contributing_sources: 贡献该答案的来源列表(去重)
        confidence: 最终置信度(0.0-1.0,按信任度加权)
        conflicts: 检测到的冲突列表
        nodes: 融合后的节点列表(便于上层展示)
    """

    answer: str = ""
    contributing_sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    conflicts: list[ConflictItem] = field(default_factory=list)
    nodes: list[KGNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "contributing_sources": list(self.contributing_sources),
            "confidence": self.confidence,
            "conflicts": [
                {
                    "fact": c.fact,
                    "sources_with_values": dict(c.sources_with_values),
                    "resolution": c.resolution,
                }
                for c in self.conflicts
            ],
            "nodes_count": len(self.nodes),
        }


@dataclass
class _SourceCandidate:
    """单条候选(来自某源 + 信任分)。"""

    node: KGNode
    source: str
    trust: float
    score: float = 0.0  # 检索相关性分数


class KnowledgeFusion:
    """多源知识融合器。

    用法:
        fusion = KnowledgeFusion(graphiti=rt, lightrag=lt, trust_scorer=ts)
        result = fusion.fuse("北京户口注销流程")
        # result.answer / result.confidence / result.conflicts / result.contributing_sources

    设计:
        - 调用 graphiti.search + lightrag.search 拉候选
        - 按信任度加权融合,选出 top 候选
        - 冲突检测:对同一 source 的不同候选,内容差异 → conflict
        - 置信度传播:加权 = trust * relevance_score
    """

    def __init__(
        self,
        graphiti: Optional[GraphitiRuntime] = None,
        lightrag: Optional[LightRAGRuntime] = None,
        trust_scorer: Optional[TrustScorer] = None,
    ) -> None:
        self.graphiti = graphiti
        self.lightrag = lightrag
        self.trust_scorer = trust_scorer or TrustScorer()
        self._lock = threading.RLock()

    def fuse(
        self,
        query: str,
        top_k: int = 5,
    ) -> FusionResult:
        """融合多源结果。

        流程:
            1. 从 graphiti / lightrag 各拉 top_k 候选
            2. 按 trust * relevance 加权排序
            3. 选出 top_n,作为最终答案(合并 content)
            4. 检测冲突:对 source prefix 相同但 content 不同的候选
            5. 计算 confidence:加权平均

        Args:
            query: 用户查询
            top_k: 每个源最多返回的候选数

        Returns:
            FusionResult
        """
        if not is_enabled("knowledge_graph"):
            return FusionResult(answer="", confidence=0.0)

        with self._lock:
            candidates: list[_SourceCandidate] = []

            # 1. 从 GraphitiRuntime 拉候选
            if self.graphiti is not None:
                kg_nodes = self.graphiti.search(query, max_depth=2, top_k=top_k)
                for n in kg_nodes:
                    src = n.properties.get("source", "unknown")
                    trust = self.trust_scorer.score(src)
                    candidates.append(_SourceCandidate(
                        node=n, source=src, trust=trust, score=1.0,
                    ))

            # 2. 从 LightRAGRuntime 拉候选
            if self.lightrag is not None:
                results: list[SearchResult] = self.lightrag.search(query, top_k=top_k)
                for r in results:
                    # LightNode → KGNode 转换
                    kg_node = KGNode(
                        id=r.node.id,
                        type="entity",
                        content=r.node.content,
                        properties={
                            "source": r.node.source,
                            "score": r.score,
                            **r.node.properties,
                        },
                    )
                    src = r.node.source or "unknown"
                    trust = self.trust_scorer.score(src)
                    candidates.append(_SourceCandidate(
                        node=kg_node, source=src, trust=trust, score=r.score,
                    ))

            if not candidates:
                return FusionResult(answer="", confidence=0.0)

            # 3. 加权排序:trust * relevance
            for c in candidates:
                c.score = c.trust * max(0.1, c.score)
            candidates.sort(key=lambda x: x.score, reverse=True)

            # 4. 选 top_n(默认全部,实际可截断)
            top_n = candidates[:max(top_k, 1)]

            # 5. 构建答案 + 贡献源
            answer_parts: list[str] = []
            contributing: list[str] = []
            nodes_out: list[KGNode] = []
            for c in top_n:
                answer_parts.append(f"[{c.source}] {c.node.content}")
                if c.source not in contributing:
                    contributing.append(c.source)
                nodes_out.append(c.node)

            answer = "\n---\n".join(answer_parts)

            # 6. 冲突检测:对同一 source prefix 但内容差异明显的候选
            conflicts = self._detect_conflicts(top_n)

            # 7. 置信度:加权平均(权重 = trust)
            trust_scores = [c.trust for c in top_n]
            confidence = self.trust_scorer.aggregate(trust_scores)

            return FusionResult(
                answer=answer,
                contributing_sources=contributing,
                confidence=confidence,
                conflicts=conflicts,
                nodes=nodes_out,
            )

    # ==================================================================
    # 冲突检测
    # ==================================================================

    def _detect_conflicts(self, candidates: list[_SourceCandidate]) -> list[ConflictItem]:
        """检测来源间的冲突。

        简化策略:
            - 把 source 按类别分组(official_law:* / court_case:* / user_experience:* 等)
            - 同类别内取最可信候选作为该类别"立场"
            - 不同类别立场不一致 → conflict
        """
        if not candidates:
            return []

        # 1. 按类别分组
        by_category: dict[str, _SourceCandidate] = {}
        for c in candidates:
            cat = self._classify_source(c.source)
            existing = by_category.get(cat)
            if existing is None or c.trust > existing.trust:
                by_category[cat] = c

        # 2. 不同类别的"立场"(content 前 50 字符)对比
        category_positions: dict[str, str] = {}
        for cat, c in by_category.items():
            content = (c.node.content or "").strip()[:50]
            category_positions[cat] = content

        # 3. 检测冲突:不同的 content → conflict
        conflicts: list[ConflictItem] = []
        unique_contents = set(category_positions.values())
        if len(unique_contents) >= 2:
            # 存在不同立场 → 冲突
            conflicts.append(ConflictItem(
                fact="content_disagreement",
                sources_with_values=dict(category_positions),
                resolution="highest_trust",
            ))

        return conflicts

    @staticmethod
    def _classify_source(source: str) -> str:
        """把 source 归类到 trust level 前缀。"""
        if not source:
            return "unverified"
        s = source.lower()
        for prefix in (
            "official_law:", "law:",
            "government_doc:", "gov:",
            "court_case:", "case:",
            "lawyer_verified:", "lawyer:",
            "user_experience:", "user:",
            "ai_generated:", "ai:",
        ):
            if s.startswith(prefix):
                # 返回类别名(去掉冒号后内容)
                return prefix.rstrip(":")
        return "unverified"


__all__ = [
    "ConflictItem",
    "FusionResult",
    "KnowledgeFusion",
]
