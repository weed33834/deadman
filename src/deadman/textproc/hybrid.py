"""混合检索 —— deep-spec 20 D.3。

BM25 + 向量 RRF（Reciprocal Rank Fusion）融合，权重可配（默认 0.5/0.5）。
向量侧通过回调注入（如 memory.vector_store），避免强依赖。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .bm25 import Bm25Index


def _rrf_score(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank + 1)


def hybrid_search(
    query: str,
    bm25: Bm25Index,
    vector_search_fn: Callable[[str, int], list[dict[str, Any]]] | None = None,
    top_k: int = 10,
    bm25_weight: float = 0.5,
    vector_weight: float = 0.5,
    doc_ids: set[str] | None = None,
) -> dict[str, Any]:
    """BM25 + 向量混合检索，RRF 融合。

    Args:
        query: 查询文本
        bm25: BM25 索引
        vector_search_fn: 向量检索回调，签名 f(query, top_k) -> [{"id":..,"score":..}]
        top_k: 返回条数
        bm25_weight / vector_weight: 两路权重（默认 0.5/0.5）
        doc_ids: 可选限定文档 id 集合（用于过滤）

    Returns:
        {"results": [{id, bm25_rank, vector_rank, rrf}], "bm25_hits":.., "vector_hits":..}
    """
    # BM25 路
    bm25_results = bm25.search(query, top_k=max(top_k * 2, 10))
    bm25_hits = [r for r in bm25_results if doc_ids is None or r["id"] in doc_ids]
    # 向量路
    vector_hits: list[dict[str, Any]] = []
    if vector_search_fn is not None:
        raw = vector_search_fn(query, top_k=max(top_k * 2, 10)) or []
        vector_hits = [
            {"id": r.get("id"), "score": r.get("score", 0.0), "text": r.get("text", "")}
            for r in raw
            if (doc_ids is None or r.get("id") in doc_ids)
        ]

    # RRF 融合
    rrf: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(bm25_hits):
        did = hit["id"]
        entry = rrf.setdefault(
            did, {"id": did, "bm25_rank": rank, "vector_rank": None, "bm25_score": hit["score"]}
        )
        entry["rrf"] = entry.get("rrf", 0.0) + bm25_weight * _rrf_score(rank)
    for rank, hit in enumerate(vector_hits):
        did = hit["id"]
        entry = rrf.setdefault(
            did,
            {"id": did, "bm25_rank": None, "vector_rank": rank, "vector_score": hit.get("score")},
        )
        entry["rrf"] = entry.get("rrf", 0.0) + vector_weight * _rrf_score(rank)
        entry["vector_rank"] = rank
        entry.setdefault("vector_score", hit.get("score"))

    ranked = sorted(rrf.values(), key=lambda e: e.get("rrf", 0.0), reverse=True)[:top_k]
    for e in ranked:
        e["rrf"] = round(e.get("rrf", 0.0), 4)
    return {
        "results": ranked,
        "bm25_hits": len(bm25_hits),
        "vector_hits": len(vector_hits),
        "weights": {"bm25": bm25_weight, "vector": vector_weight},
    }
