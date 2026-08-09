"""P8.3 知识图谱与 RAG - 时序知识图 + 向量检索 + 多源融合 + 跨用户匿名化。

模块结构:
    - graphiti_runtime.py: Graphiti 风格时序知识图(temporal KG,带 valid_from/to)
    - lightrag_runtime.py: LightRAG 风格向量检索(简化,hash embedding 降级)
    - freshness.py: 知识时效管理(法规变更检测 + 归档)
    - trust.py: 多源信任度分级(7 个预设等级 + 动态调整)
    - fusion.py: 多源融合(Graphiti + LightRAG + 向量库,按信任度加权)
    - private_graph.py: 用户 / 租户私有知识图(强隔离)
    - anonymizer.py: 跨用户匿名化(k-匿名 + l-多样性)
    - manager.py: 高层 KnowledgeManager 单例(编排所有子模块)

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 重依赖(graphiti / networkx / chromadb / sentence-transformers)必须 OPTIONAL
    - 默认走纯内存降级后端(hash embedding + dict-of-dicts 图)
    - 原子写 + 线程安全
    - PII 脱敏(PIIRedactor)在 add_knowledge 入口强制执行

法规依据:
    - PIPL 第 9 条:个人信息处理应"目的明确、最小必要"
    - 中国《生成式 AI 管理办法》第 9 条:训练数据来源合法、标注真实
    - GDPR 第 25 条:数据保护设计与默认隐私
"""

from __future__ import annotations

from .anonymizer import (
    QUASI_IDENTIFIERS,
    SENSITIVE_ATTRIBUTES,
    AnonymizationResult,
    Anonymizer,
)
from .freshness import (
    CATEGORY_TTL_DAYS,
    ExternalSource,
    FreshnessReport,
    KnowledgeCategory,
    KnowledgeFreshness,
)
from .fusion import ConflictItem, FusionResult, KnowledgeFusion
from .graphiti_runtime import (
    Episode,
    EpisodeType,
    GraphitiRuntime,
    KGEdge,
    KGNode,
)
from .lightrag_runtime import LightNode, LightRAGRuntime, SearchResult
from .manager import (
    KnowledgeDisabledError,
    KnowledgeManager,
    get_knowledge_manager,
    reset_knowledge_manager,
)
from .private_graph import PrivateGraph, TenantIsolationError
from .trust import TrustLevel, TrustRecord, TrustScorer

__all__ = [
    # graphiti_runtime
    "Episode",
    "EpisodeType",
    "KGNode",
    "KGEdge",
    "GraphitiRuntime",
    # lightrag_runtime
    "LightNode",
    "SearchResult",
    "LightRAGRuntime",
    # freshness
    "KnowledgeCategory",
    "CATEGORY_TTL_DAYS",
    "FreshnessReport",
    "ExternalSource",
    "KnowledgeFreshness",
    # trust
    "TrustLevel",
    "TrustRecord",
    "TrustScorer",
    # fusion
    "ConflictItem",
    "FusionResult",
    "KnowledgeFusion",
    # private_graph
    "PrivateGraph",
    "TenantIsolationError",
    # anonymizer
    "Anonymizer",
    "AnonymizationResult",
    "QUASI_IDENTIFIERS",
    "SENSITIVE_ATTRIBUTES",
    # manager
    "KnowledgeManager",
    "KnowledgeDisabledError",
    "get_knowledge_manager",
    "reset_knowledge_manager",
]
