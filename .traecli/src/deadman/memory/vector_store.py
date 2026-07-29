"""向量库抽象层 - P2.1 真实 embedding 检索。

设计目标:
    - 抽象 VectorStore 基类,统一 add/query/delete 接口
    - ChromaVectorStore 包装 chromadb(可选依赖,try import)
    - InMemoryVectorStore 纯 Python dict + 余弦相似度(默认降级后端)
    - embedding 模型优先 sentence-transformers/BAAI/bge-small-zh-v1.5,
      不可用时降级到 hash 模拟 embedding(hashlib.sha256 + 分词)

三大铁律:
    1. feature flag 默认关闭:DEADMAN_VECTOR_STORE_ENABLED=0
    2. 降级路径全覆盖:chromadb 不可用 → InMemoryVectorStore;
       sentence-transformers 不可用 → hash 模拟 embedding
    3. 不破坏现有测试:关闭时 episodic.recall_by_semantic 走旧关键词匹配路径
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..utils.text_similarity import tokenize_for_embedding

logger = logging.getLogger(__name__)

# =====================================================================
# feature flag - 默认关闭
# =====================================================================
VECTOR_STORE_ENABLED: bool = os.environ.get(
    "DEADMAN_VECTOR_STORE_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# embedding 维度(hash 模拟用)
_HASH_EMBEDDING_DIM: int = 256

# =====================================================================
# 可选依赖 - chromadb 与 sentence-transformers,缺失时降级
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


def _hash_embedding(text: str, dim: int = _HASH_EMBEDDING_DIM) -> list[float]:
    """hash 模拟 embedding - 用 sha256 分桶 + L2 归一化。

    保证:
        - 同样文本生成同样向量(确定性)
        - 不同文本生成不同向量(分布尽量均匀)
        - 输出 L2 归一化,余弦相似度内积等价
    """
    vec = [0.0] * dim
    tokens = tokenize_for_embedding(text)
    if not tokens:
        # 退化:对原文 hash 至少保证非零
        tokens = [text.lower()]
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        # 用前 4 字节作为桶索引
        bucket = int.from_bytes(h[:4], "big") % dim
        # 用后续字节确定符号与权重
        sign = 1.0 if (h[4] & 1) == 0 else -1.0
        weight = 1.0 + (h[5] % 10) / 10.0  # 1.0 ~ 1.9
        vec[bucket] += sign * weight
    # L2 归一化(零向量保持为零)
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。a/b 已归一化时等价内积;此处仍做完整计算以防未归一化输入。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# =====================================================================
# Embedding 函数 - 优先 sentence-transformers,降级到 hash
# =====================================================================

class _EmbeddingFunc:
    """embedding 函数封装。优先 sentence-transformers;不可用降级到 hash。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        self.model_name = model_name
        self._model: Any = None
        self._use_st = False
        if _HAS_ST:
            try:
                self._model = SentenceTransformer(model_name)  # type: ignore[call-arg]
                self._use_st = True
                logger.info("sentence-transformers 加载成功: %s", model_name)
            except Exception as exc:  # pragma: no cover - 加载失败降级
                logger.warning(
                    "sentence-transformers 加载失败,降级到 hash embedding: %s", exc
                )
                self._model = None
                self._use_st = False

    def embed(self, text: str) -> list[float]:
        """对单条文本生成 embedding 向量"""
        if self._use_st and self._model is not None:
            try:
                vec = self._model.encode(text, normalize_embeddings=True)
                return [float(x) for x in vec.tolist()]
            except Exception as exc:  # pragma: no cover - 运行时失败降级
                logger.warning("ST encode 失败,降级到 hash: %s", exc)
        return _hash_embedding(text)

    @property
    def using_real_model(self) -> bool:
        return self._use_st and self._model is not None


# =====================================================================
# VectorStore 抽象基类
# =====================================================================

class VectorStore(ABC):
    """向量库抽象基类"""

    @abstractmethod
    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None:
        """添加一条向量。同 id 覆盖。"""

    @abstractmethod
    def query(
        self, text: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """查询最相似的 top_k 条,返回 [{id, score, metadata}]"""

    @abstractmethod
    def delete(self, id: str) -> None:
        """按 id 删除一条向量"""

    @abstractmethod
    def count(self) -> int:
        """当前向量数"""


# =====================================================================
# InMemoryVectorStore - 纯 Python dict + 余弦相似度(默认降级后端)
# =====================================================================

class InMemoryVectorStore(VectorStore):
    """纯 Python 实现的向量库 - 默认降级后端。

    embedding 用 hash 模拟(无外部依赖),余弦相似度计算。
    """

    def __init__(self, embedding_func: Optional[_EmbeddingFunc] = None) -> None:
        self._embed: _EmbeddingFunc = embedding_func or _EmbeddingFunc()
        # id -> {"vector": list[float], "text": str, "metadata": dict}
        self._store: dict[str, dict[str, Any]] = {}

    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None:
        vec = self._embed.embed(text)
        self._store[id] = {
            "vector": vec,
            "text": text,
            "metadata": dict(metadata) if metadata else {},
        }

    def query(
        self, text: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        if not self._store:
            return []
        qvec = self._embed.embed(text)
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for eid, entry in self._store.items():
            score = _cosine_similarity(qvec, entry["vector"])
            scored.append((score, eid, entry["metadata"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": eid, "score": score, "metadata": meta}
            for score, eid, meta in scored[:top_k]
        ]

    def delete(self, id: str) -> None:
        self._store.pop(id, None)

    def count(self) -> int:
        return len(self._store)


# =====================================================================
# ChromaVectorStore - 包装 chromadb(可选依赖)
# =====================================================================

class ChromaVectorStore(VectorStore):
    """chromadb 包装 - 需 chromadb 已安装。

    embedding 默认用 sentence-transformers,降级到 chromadb 自带的
    DefaultEmbeddingFunction(同样依赖 onnx 等)。所有外部依赖都不可用
    时,工厂函数会直接返回 InMemoryVectorStore,不会进入本类。
    """

    def __init__(
        self,
        collection_name: str = "deadman_episodes",
        persist_path: Optional[str] = None,
        embedding_func: Optional[_EmbeddingFunc] = None,
    ) -> None:
        if not _HAS_CHROMADB:
            raise RuntimeError("chromadb 未安装,请用 InMemoryVectorStore")
        self._embedding_func = embedding_func or _EmbeddingFunc()
        # chromadb 客户端
        if persist_path:
            self._client = chromadb.PersistentClient(path=persist_path)  # type: ignore[union-attr]
        else:
            self._client = chromadb.Client()  # type: ignore[union-attr]
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None:
        vec = self._embedding_func.embed(text)
        # chromadb 要求 metadata 值为基本类型
        safe_meta = self._sanitize_metadata(metadata)
        self._collection.upsert(
            ids=[id],
            embeddings=[vec],
            documents=[text],
            metadatas=[safe_meta],
        )

    def query(
        self, text: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        if self.count() == 0:
            return []
        qvec = self._embedding_func.embed(text)
        res = self._collection.query(
            query_embeddings=[qvec],
            n_results=top_k,
        )
        ids = (res.get("ids") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[dict[str, Any]] = []
        for i, eid in enumerate(ids):
            distance = dists[i] if i < len(dists) else 0.0
            # chromadb cosine distance = 1 - similarity
            score = max(0.0, 1.0 - float(distance))
            meta = metas[i] if i < len(metas) else {}
            out.append({"id": eid, "score": score, "metadata": meta})
        return out

    def delete(self, id: str) -> None:
        self._collection.delete(ids=[id])

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception:
            return 0

    @staticmethod
    def _sanitize_metadata(meta: Optional[dict]) -> dict[str, Any]:
        """chromadb metadata 值必须是 str/int/float/bool/None"""
        if not meta:
            return {}
        safe: dict[str, Any] = {}
        for k, v in meta.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                safe[str(k)] = v
            else:
                safe[str(k)] = str(v)
        return safe


# =====================================================================
# 工厂函数 - 按 feature flag 与依赖可用性返回实例
# =====================================================================

# 模块级单例(避免反复初始化 ST 模型,启动开销大)
_vector_store_singleton: Optional[VectorStore] = None


def get_vector_store(force_refresh: bool = False) -> Optional[VectorStore]:
    """工厂函数:按 feature flag 与依赖可用性返回 VectorStore 实例。

    优先级:
        1. DEADMAN_VECTOR_STORE_ENABLED=0 → 返回 None(走旧关键词匹配路径)
        2. chromadb 可用 → ChromaVectorStore
        3. chromadb 不可用 → InMemoryVectorStore(纯降级)

    返回 None 表示功能未启用,调用方应自行走旧路径。
    """
    global _vector_store_singleton
    if not VECTOR_STORE_ENABLED:
        return None
    if _vector_store_singleton is not None and not force_refresh:
        return _vector_store_singleton
    try:
        if _HAS_CHROMADB:
            try:
                _vector_store_singleton = ChromaVectorStore()
                logger.info("VectorStore 启用: ChromaVectorStore")
                return _vector_store_singleton
            except Exception as exc:
                logger.warning(
                    "ChromaVectorStore 初始化失败,降级到 InMemory: %s", exc
                )
        _vector_store_singleton = InMemoryVectorStore()
        logger.info("VectorStore 启用: InMemoryVectorStore")
        return _vector_store_singleton
    except Exception as exc:  # pragma: no cover - 极端情况
        logger.warning("get_vector_store 失败,返回 None: %s", exc)
        return None


def reset_vector_store_singleton() -> None:
    """测试辅助:重置模块级单例"""
    global _vector_store_singleton
    _vector_store_singleton = None


__all__ = [
    "VectorStore",
    "InMemoryVectorStore",
    "ChromaVectorStore",
    "get_vector_store",
    "reset_vector_store_singleton",
    "VECTOR_STORE_ENABLED",
    "_EmbeddingFunc",
    "_hash_embedding",
    "_cosine_similarity",
]
