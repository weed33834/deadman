"""P8.3.2 LightRAG-style 向量检索运行时(简化版,无图结构)。

设计目标:
    - 提供与 GraphitiRuntime 相似 API 的"轻量级"替代后端
    - 重依赖 lightrag / chromadb 必须 OPTIONAL:不可用降级到纯内存向量
    - 用 hash 模拟 embedding(无外部依赖,确定性,可测试)
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭

与 GraphitiRuntime 的差异:
    - 无图结构,仅按向量余弦相似度检索
    - 不支持 get_temporal(节点不带时序)
    - 适合"知识片段检索"场景(法规原文 / 案例文书)
    - 由 KnowledgeFusion 与 GraphitiRuntime 联合使用,取长补短

设计原则:
    - 三大铁律:flag 关闭走降级、重依赖 lazy import、不破坏现有测试
    - 原子写:持久化用 .tmp + os.replace
    - 线程安全:threading.RLock 守护内存库
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..infrastructure.feature_flags import is_enabled
from ..utils.text_similarity import tokenize_for_embedding

logger = logging.getLogger(__name__)


# =====================================================================
# 可选依赖:lightrag / chromadb,缺失时降级到内存向量
# =====================================================================
try:  # pragma: no cover - 可选依赖
    import chromadb  # type: ignore

    _HAS_CHROMADB = True
except Exception:  # pragma: no cover
    chromadb = None  # type: ignore
    _HAS_CHROMADB = False

try:  # pragma: no cover - 可选依赖
    from sentence_transformers import SentenceTransformer  # type: ignore

    _HAS_ST = True
except Exception:  # pragma: no cover
    SentenceTransformer = None  # type: ignore
    _HAS_ST = False


# =====================================================================
# hash embedding(降级后端,使用共享 tokenize_for_embedding)
# =====================================================================

_HASH_EMBEDDING_DIM: int = 256


def _hash_embedding(text: str, dim: int = _HASH_EMBEDDING_DIM) -> list[float]:
    """hash 模拟 embedding:L2 归一化,余弦相似度内积等价。"""
    vec = [0.0] * dim
    tokens = tokenize_for_embedding(text)
    if not tokens:
        tokens = [text.lower()]
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        bucket = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if (h[4] & 1) == 0 else -1.0
        weight = 1.0 + (h[5] % 10) / 10.0
        vec[bucket] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# =====================================================================
# 数据模型(复用 graphiti_runtime 的 KGNode,避免循环导入,这里独立定义 LightNode)
# =====================================================================


@dataclass
class LightNode:
    """LightRAG 节点(向量库中的一条记录)。

    与 KGNode 区别:不带时序(无 valid_from/valid_to),仅做向量检索。

    Attributes:
        id: 节点 ID(稳定唯一)
        content: 文本内容
        source: 来源标识
        properties: 附加属性
        embedding: 缓存的 embedding 向量(可选;为空时按需计算)
    """

    id: str
    content: str
    source: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LightNode:
        return cls(
            id=data["id"],
            content=data.get("content", ""),
            source=data.get("source", ""),
            properties=data.get("properties", {}) or {},
            embedding=data.get("embedding", []) or [],
        )


@dataclass
class SearchResult:
    """单条向量检索结果。

    Attributes:
        node: 命中的 LightNode
        score: 相似度分数(0-1)
    """

    node: LightNode
    score: float = 0.0


# =====================================================================
# LightRAGRuntime - 向量检索运行时
# =====================================================================


class LightRAGRuntime:
    """LightRAG 风格向量检索运行时(轻量替代 Graphiti)。

    用法:
        rt = LightRAGRuntime()
        nid = rt.add(content="...", source="...")
        results = rt.search("查询", top_k=5)

    设计:
        - 重依赖 chromadb / sentence-transformers 必须 lazy import
        - 默认 InMemoryVectorStore:hash embedding + 余弦相似度
        - 持久化可选:不传 persist_path 纯内存
        - 线程安全:RLock 守护 add/search
    """

    def __init__(
        self,
        persist_path: Path | None = None,
        use_chroma: bool = False,
    ) -> None:
        """构造运行时。

        Args:
            persist_path: 持久化路径;None 纯内存
            use_chroma: True 时尝试 chromadb;False 默认走内存向量库
        """
        self.persist_path = persist_path
        self._lock = threading.RLock()
        self._nodes: dict[str, LightNode] = {}
        self._use_chroma = False
        self._chroma_collection = None

        if use_chroma and _HAS_CHROMADB:
            try:
                if persist_path:
                    self._chroma_client = chromadb.PersistentClient(path=str(persist_path))  # type: ignore[union-attr]
                else:
                    self._chroma_client = chromadb.Client()  # type: ignore[union-attr]
                self._chroma_collection = self._chroma_client.get_or_create_collection(
                    name="deadman_knowledge",
                    metadata={"hnsw:space": "cosine"},
                )
                self._use_chroma = True
                logger.info("LightRAGRuntime: ChromaVectorStore 已启用")
            except Exception as e:  # pragma: no cover - chroma 初始化失败降级
                logger.warning("chroma init failed, fallback to in-memory: %s", e)
                self._use_chroma = False
                self._chroma_collection = None

        # persist_path 用于内存模式的落盘(chroma 模式自带持久化)
        if persist_path is not None and not self._use_chroma:
            self._load()

    # ==================================================================
    # Add API
    # ==================================================================

    def add(
        self,
        content: str,
        source: str = "",
        properties: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> str:
        """添加一条知识到向量库。

        Returns:
            node_id(UUID)
        """
        if not is_enabled("knowledge_graph"):
            logger.debug("knowledge_graph disabled, content not stored")
            return str(uuid4())

        with self._lock:
            nid = node_id or f"lnode-{uuid4()}"
            node = LightNode(
                id=nid,
                content=content,
                source=source,
                properties=properties or {},
                embedding=_hash_embedding(content) if not self._use_chroma else [],
            )

            if self._use_chroma and self._chroma_collection is not None:
                # chromadb 模式
                self._chroma_collection.upsert(
                    ids=[nid],
                    embeddings=[_hash_embedding(content)],
                    documents=[content],
                    metadatas=[{"source": source, **(properties or {})}],
                )
            else:
                self._nodes[nid] = node
                self._persist()
            return nid

    def add_node(self, node: LightNode) -> str:
        """直接添加一个 LightNode。"""
        with self._lock:
            if not node.embedding:
                node.embedding = _hash_embedding(node.content)
            if self._use_chroma and self._chroma_collection is not None:
                self._chroma_collection.upsert(
                    ids=[node.id],
                    embeddings=[node.embedding],
                    documents=[node.content],
                    metadatas=[{"source": node.source, **node.properties}],
                )
            else:
                self._nodes[node.id] = node
                self._persist()
            return node.id

    def delete(self, node_id: str) -> bool:
        """按 ID 删除。"""
        with self._lock:
            if self._use_chroma and self._chroma_collection is not None:
                self._chroma_collection.delete(ids=[node_id])
                return True
            if node_id in self._nodes:
                del self._nodes[node_id]
                self._persist()
                return True
            return False

    # ==================================================================
    # Search API
    # ==================================================================

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[SearchResult]:
        """向量相似度检索。

        Args:
            query: 查询文本
            top_k: 最多返回数
            min_score: 最低相似度阈值

        Returns:
            SearchResult 列表(按 score 降序)
        """
        if not is_enabled("knowledge_graph"):
            return []

        with self._lock:
            if self._use_chroma and self._chroma_collection is not None:
                try:
                    res = self._chroma_collection.query(
                        query_embeddings=[_hash_embedding(query)],
                        n_results=top_k,
                    )
                    ids = (res.get("ids") or [[]])[0]
                    metas = (res.get("metadatas") or [[]])[0]
                    dists = (res.get("distances") or [[]])[0]
                    docs = (res.get("documents") or [[]])[0]
                    out: list[SearchResult] = []
                    for i, eid in enumerate(ids):
                        distance = dists[i] if i < len(dists) else 0.0
                        score = max(0.0, 1.0 - float(distance))
                        if score < min_score:
                            continue
                        node = LightNode(
                            id=eid,
                            content=docs[i] if i < len(docs) else "",
                            source=(metas[i].get("source", "") if i < len(metas) else ""),
                            properties=(metas[i] if i < len(metas) else {}) or {},
                        )
                        out.append(SearchResult(node=node, score=score))
                    return out
                except Exception as e:  # pragma: no cover
                    logger.warning("chroma query failed, fallback to in-memory: %s", e)

            # 内存模式:余弦相似度
            if not self._nodes:
                return []
            qvec = _hash_embedding(query)
            scored: list[tuple[float, LightNode]] = []
            for node in self._nodes.values():
                if not node.embedding:
                    node.embedding = _hash_embedding(node.content)
                score = _cosine_similarity(qvec, node.embedding)
                scored.append((score, node))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [SearchResult(node=n, score=s) for s, n in scored[:top_k] if s >= min_score]

    # ==================================================================
    # 工具方法
    # ==================================================================

    def count(self) -> int:
        with self._lock:
            if self._use_chroma and self._chroma_collection is not None:
                try:
                    return int(self._chroma_collection.count())
                except Exception:
                    return 0
            return len(self._nodes)

    def all_nodes(self) -> list[LightNode]:
        with self._lock:
            return list(self._nodes.values())

    def get_node(self, node_id: str) -> LightNode | None:
        with self._lock:
            return self._nodes.get(node_id)

    # ==================================================================
    # 持久化(仅内存模式)
    # ==================================================================

    def _persist(self) -> None:
        """原子落盘(.tmp + os.replace)。"""
        if self.persist_path is None or self._use_chroma:
            return
        try:
            data = {
                "version": 1,
                "updated_at": time.time(),
                "nodes": [n.to_dict() for n in self._nodes.values()],
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.persist_path)
        except OSError as e:
            logger.error("LightRAGRuntime persist failed: %s", e)

    def _load(self) -> None:
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            text = self.persist_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            for nd in data.get("nodes", []):
                node = LightNode.from_dict(nd)
                self._nodes[node.id] = node
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("LightRAGRuntime load failed, using empty: %s", e)


__all__ = [
    "LightNode",
    "LightRAGRuntime",
    "SearchResult",
    "_cosine_similarity",
    "_hash_embedding",
]
