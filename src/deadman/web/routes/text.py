"""文本处理与检索 API —— /api/text/*（deep-spec 20 底层能力落地）

把 textproc 底层算法暴露为真实 API，供管理台「文本分析」面板与各模块复用：
  * GET  /api/text/status        -> 能力状态（jieba 可用性）
  * POST /api/text/keywords      -> 关键词提取（TF-IDF + TextRank）
  * POST /api/text/analyze       -> 一键文本分析（清洗/分词/句子/关键词/长度）
  * GET  /api/text/index         -> 列出已建 BM25 索引（从知识库构建）
  * POST /api/text/search        -> 混合检索（BM25 + 可选向量 RRF）
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/text", tags=["textproc"])

_bm25_cache: dict[str, Any] = {}


def _knowledge_docs() -> dict[str, str]:
    docs: dict[str, str] = {}
    regions_dir = settings.knowledge_dir / "regions"
    if not regions_dir.exists():
        return docs
    for md in regions_dir.rglob("*.md"):
        try:
            content = md.read_text(encoding="utf-8")
            if content.strip():
                docs[md.relative_to(settings.project_root).as_posix()] = content
        except OSError:
            continue
    return docs


def _get_bm25_index(rebuild: bool = False) -> tuple[Any, int]:
    from ...textproc.bm25 import Bm25Index

    entry = _bm25_cache.get("knowledge")
    if entry is None or rebuild:
        idx = Bm25Index()
        docs = _knowledge_docs()
        idx.add_batch(docs)
        entry = {"index": idx, "docs": len(docs)}
        _bm25_cache["knowledge"] = entry
    return entry["index"], entry["docs"]


@router.get("/status")
async def text_status() -> dict[str, Any]:
    from ...textproc.tokenize import jieba_available

    _, doc_count = _get_bm25_index()
    return {
        "jieba_available": jieba_available(),
        "knowledge_docs": doc_count,
        "capabilities": ["tokenize", "clean", "keywords", "similarity", "bm25", "hybrid"],
    }


@router.post("/keywords")
async def text_keywords(
    text: str = Body(default=None, embed=True, description="输入文本"),
    top_n: int = Body(default=5, ge=1, le=50),
) -> dict[str, Any]:
    from ...textproc import extract_keywords

    keywords = extract_keywords(text or "", top_n=top_n)
    return {"ok": True, "keywords": keywords}


@router.post("/analyze")
async def text_analyze(
    text: str = Body(default=None, embed=True, description="输入文本"),
) -> dict[str, Any]:
    from ...textproc import (
        clean_text,
        extract_keywords,
        remove_stopwords,
        split_sentences,
        tokenize_words,
    )
    from ...textproc.tokenize import jieba_available

    cleaned = clean_text(text or "")
    words = remove_stopwords(tokenize_words(cleaned))
    return {
        "ok": True,
        "char_count": len(text or ""),
        "word_count": len(words),
        "sentence_count": len(split_sentences(cleaned)),
        "cleaned": cleaned,
        "words": words[:200],
        "keywords": extract_keywords(text or "", top_n=8),
        "jieba": jieba_available(),
    }


@router.get("/index")
async def text_index() -> dict[str, Any]:
    _, doc_count = _get_bm25_index()
    return {
        "ok": True,
        "key": "knowledge",
        "doc_count": doc_count,
        "sample_docs": list(_knowledge_docs().keys())[:10],
    }


@router.post("/search")
async def text_search(
    query: str = Body(default=None, embed=True, description="查询文本"),
    top_k: int = Body(default=10, ge=1, le=50),
    use_vector: bool = Body(default=False, description="是否启用向量混合"),
) -> dict[str, Any]:
    from ...textproc import hybrid_search

    idx, doc_count = _get_bm25_index()
    if doc_count == 0:
        raise HTTPException(status_code=404, detail="知识库为空，无法检索")
    vector_fn = None
    if use_vector:

        def _vec(query: str, top_k: int):
            try:
                from ...memory.vector_store import get_vector_store

                store = get_vector_store()
                if store is None:
                    return []
                return [
                    {"id": r.get("id"), "score": r.get("score", 0.0)}
                    for r in store.search(query, top_k)
                ]
            except Exception:
                return []

        vector_fn = _vec
    result = hybrid_search(query or "", idx, vector_search_fn=vector_fn, top_k=top_k)
    result["doc_count"] = doc_count
    result["query"] = query
    return {"ok": True, **result}
