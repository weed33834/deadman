"""P8.3.9 高层 KnowledgeManager - 编排所有知识子模块。

设计目标:
    - 单例入口(KnowledgeManager),供 CLI / agent / web 调用
    - 编排:graphiti + lightrag + freshness + trust + fusion + private_graph + anonymizer
    - 端到端:query(question, user_id, tenant_id) → FusionResult
    - add_knowledge(content, source, tenant_id) 写入路径:
        1. PII redaction(PIIRedactor)
        2. graphiti.add_episode + lightrag.add
        3. freshness.touch 记录时效
        4. trust 自动按 source 分类
    - flag 关闭:KnowledgeDisabledError 或空结果

法规依据:
    - PIPL 第 9 条:处理个人信息应"目的明确、最小必要"
    - 知识图谱应支持多租户隔离 + PII 脱敏

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 原子写 + 线程安全
    - 重依赖 lazy import
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from ..infrastructure.defense.pii_guard import (
    PIIRedactor,
    RedactStrategy,
    get_pii_redactor,
)
from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import (
    DEFAULT_TENANT_ID,
    get_current_tenant_id,
)
from .anonymizer import Anonymizer
from .freshness import KnowledgeFreshness
from .fusion import FusionResult, KnowledgeFusion
from .graphiti_runtime import (
    Episode,
    EpisodeType,
    GraphitiRuntime,
    KGNode,
)
from .lightrag_runtime import LightRAGRuntime
from .private_graph import PrivateGraph, TenantIsolationError
from .trust import TrustScorer

logger = logging.getLogger(__name__)


class KnowledgeDisabledError(Exception):
    """知识图谱功能被 feature flag 关闭时抛出。"""


class KnowledgeManager:
    """高层知识管理器(单例)。

    用法:
        km = get_knowledge_manager()
        # 1. 添加知识(自动 PII 脱敏)
        km.add_knowledge("北京户口注销需提供死亡证明", source="official_law:cn")
        # 2. 查询
        result = km.query("户口注销流程", user_id="u1", tenant_id="t1")
        # 3. 检查时效
        reports = km.check_freshness_all()

    设计:
        - 持有 graphiti / lightrag / freshness / trust / fusion / anonymizer 引用
        - 每个用户的 PrivateGraph 在 _private_graphs[(tenant_id, user_id)] 缓存
        - flag 关闭:query 返回空 FusionResult;add_knowledge 抛 KnowledgeDisabledError
        - PII 脱敏:复用 PIIRedactor(default PARTIAL 策略)
    """

    def __init__(
        self,
        persist_root: Optional[Path] = None,
        graphiti: Optional[GraphitiRuntime] = None,
        lightrag: Optional[LightRAGRuntime] = None,
        pii_redactor: Optional[PIIRedactor] = None,
    ) -> None:
        """构造。

        Args:
            persist_root: 持久化根目录;None 时使用默认 data/knowledge/
            graphiti: 注入 GraphitiRuntime;None 时按默认路径创建
            lightrag: 注入 LightRAGRuntime;None 时按默认路径创建
            pii_redactor: PII 脱敏器;None 时用全局单例
        """
        self.persist_root = persist_root or Path("data/knowledge")
        self.persist_root.mkdir(parents=True, exist_ok=True)

        # 子模块(惰性初始化或注入)
        self.graphiti = graphiti or GraphitiRuntime(
            persist_path=self.persist_root / "graphiti.json",
        )
        self.lightrag = lightrag or LightRAGRuntime(
            persist_path=self.persist_root / "lightrag.json",
        )
        self.trust = TrustScorer(
            persist_path=self.persist_root / "trust.json",
        )
        self.freshness = KnowledgeFreshness(
            persist_path=self.persist_root / "freshness.json",
        )
        self.fusion = KnowledgeFusion(
            graphiti=self.graphiti,
            lightrag=self.lightrag,
            trust_scorer=self.trust,
        )
        self.anonymizer = Anonymizer()
        self.pii_redactor = pii_redactor or get_pii_redactor()

        self._lock = threading.RLock()
        # (tenant_id, user_id) -> PrivateGraph
        self._private_graphs: dict[tuple[str, str], PrivateGraph] = {}

    # ==================================================================
    # 添加知识(PII 脱敏 + 多后端写入)
    # ==================================================================

    def add_knowledge(
        self,
        content: str,
        source: str = "",
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """添加一条知识。

        流程:
            1. flag 关闭 → 抛 KnowledgeDisabledError
            2. PII 脱敏(PIIRedactor.redact)
            3. graphiti.add_episode(content, source)
            4. lightrag.add(content, source)
            5. freshness.touch(knowledge_id, source)
            6. 若指定 user_id → 写入对应 PrivateGraph

        Args:
            content: 知识内容(自然语言文本)
            source: 来源标识(如 "official_law:cn")
            tenant_id: 租户 ID(默认走 get_current_tenant_id)
            user_id: 用户 ID(指定则同时写入私有图)
            metadata: 附加元信息

        Returns:
            knowledge_id(用于后续追溯)
        """
        if not is_enabled("knowledge_graph"):
            raise KnowledgeDisabledError(
                "knowledge_graph feature flag disabled; "
                "set DEADMAN_KNOWLEDGE_GRAPH_ENABLED=1 to enable"
            )

        tid = tenant_id or get_current_tenant_id() or DEFAULT_TENANT_ID
        knowledge_id = f"k-{uuid4()}"

        # 1. PII 脱敏(用默认策略,即各类型 PII 的预设策略)
        pii_result = self.pii_redactor.redact(content, default_strategy=RedactStrategy.PARTIAL)
        safe_content = pii_result.redacted_text
        if pii_result.has_pii:
            logger.info(
                "PII redacted before storage: %d matches",
                len(pii_result.matches),
            )

        # 2. 写入 graphiti
        episode = Episode(
            content=safe_content,
            source=source,
            type=EpisodeType.TEXT,
            metadata={
                "knowledge_id": knowledge_id,
                "tenant_id": tid,
                **(metadata or {}),
            },
        )
        self.graphiti.add_episode(episode)

        # 3. 写入 lightrag
        self.lightrag.add(
            content=safe_content,
            source=source,
            properties={
                "knowledge_id": knowledge_id,
                "tenant_id": tid,
                **(metadata or {}),
            },
        )

        # 4. 写入私有图(若指定 user_id)
        if user_id is not None:
            pg = self._get_or_create_private_graph(tid, user_id)
            pg.add_node(KGNode(
                id=knowledge_id,
                type="entity",
                content=safe_content,
                properties={
                    "source": source,
                    "tenant_id": tid,
                    "user_id": user_id,
                    **(metadata or {}),
                },
            ))

        # 5. 时效记录
        self.freshness.touch(knowledge_id, source=source)

        return knowledge_id

    # ==================================================================
    # 查询(端到端 fusion)
    # ==================================================================

    def query(
        self,
        question: str,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        include_private: bool = True,
        top_k: int = 5,
    ) -> FusionResult:
        """端到端查询:融合 graphiti + lightrag + 私有图。

        Args:
            question: 用户问题
            user_id: 用户 ID(指定则合并私有图结果)
            tenant_id: 租户 ID
            include_private: 是否合并私有图
            top_k: 每源最多返回数

        Returns:
            FusionResult
        """
        if not is_enabled("knowledge_graph"):
            raise KnowledgeDisabledError(
                "knowledge_graph feature flag disabled"
            )

        tid = tenant_id or get_current_tenant_id() or DEFAULT_TENANT_ID

        # 1. 全局 fusion(graphiti + lightrag)
        result = self.fusion.fuse(question, top_k=top_k)

        # 2. 合并私有图(若指定 user_id)
        if include_private and user_id is not None:
            try:
                pg = self._get_or_create_private_graph(tid, user_id)
                private_nodes = pg.query(question, top_k=top_k)
                for n in private_nodes:
                    result.nodes.append(n)
                    src = n.properties.get("source", "private")
                    if src not in result.contributing_sources:
                        result.contributing_sources.append(src)
                    # 私有图结果直接拼到答案
                    result.answer = (
                        f"[private] {n.content}\n---\n" + result.answer
                        if result.answer
                        else f"[private] {n.content}"
                    )
            except TenantIsolationError as e:
                logger.warning("Private graph access denied: %s", e)

        return result

    # ==================================================================
    # 时效检查
    # ==================================================================

    def check_freshness_all(self) -> list:
        """检查所有知识的时效。"""
        if not is_enabled("knowledge_graph"):
            return []
        return self.freshness.check_all()

    def archive_outdated(self) -> int:
        """批量归档过期知识(委托给 freshness)。"""
        return self.freshness.archive_outdated()

    # ==================================================================
    # 私有图管理
    # ==================================================================

    def _get_or_create_private_graph(
        self,
        tenant_id: str,
        user_id: str,
    ) -> PrivateGraph:
        key = (tenant_id, user_id)
        with self._lock:
            pg = self._private_graphs.get(key)
            if pg is None:
                pg = PrivateGraph(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    persist_root=self.persist_root / "private",
                )
                self._private_graphs[key] = pg
            return pg

    def get_private_graph(
        self,
        tenant_id: str,
        user_id: str,
    ) -> PrivateGraph:
        """获取指定用户的私有图(供上层直接操作)。"""
        return self._get_or_create_private_graph(tenant_id, user_id)

    # ==================================================================
    # 匿名化(供跨用户共享前调用)
    # ==================================================================

    def anonymize_for_sharing(
        self,
        node: KGNode,
        k: int = 5,
        l: int = 2,
    ):
        """对节点做匿名化(供跨用户共享)。"""
        return self.anonymizer.anonymize(node, k=k, l=l)

    # ==================================================================
    # 信任度更新(供用户反馈机制接入)
    # ==================================================================

    def update_trust(
        self,
        source: str,
        delta: float,
        reason: str = "",
    ) -> float:
        """基于用户反馈调整来源信任度。"""
        return self.trust.update(source, delta, reason)


# =====================================================================
# 单例
# =====================================================================

_km_instance: Optional[KnowledgeManager] = None
_km_lock = threading.Lock()


def get_knowledge_manager() -> KnowledgeManager:
    """获取全局 KnowledgeManager 单例(惰性初始化)。"""
    global _km_instance
    if _km_instance is None:
        with _km_lock:
            if _km_instance is None:
                _km_instance = KnowledgeManager()
    return _km_instance


def reset_knowledge_manager() -> None:
    """测试辅助:重置单例。"""
    global _km_instance
    _km_instance = None


__all__ = [
    "KnowledgeManager",
    "KnowledgeDisabledError",
    "get_knowledge_manager",
    "reset_knowledge_manager",
]
