"""BM25 检索 —— deep-spec 20 D.1。

关键词侧标配 BM25，内部基于成熟库 rank-bm25（BM25Okapi），
不再维护手写 IDF/词频实现。中文文档需先分词后索引（tokenize_words）。
"""

from __future__ import annotations

from typing import Any

from rank_bm25 import BM25Okapi

from .tokenize import tokenize_words


class Bm25Index:
    """BM25 索引（基于 rank_bm25.BM25Okapi 的轻量封装）。

    用法::

        idx = Bm25Index()
        idx.add("doc1", "身后事办理流程说明")
        idx.add("doc2", "死亡证明办理需要哪些材料")
        results = idx.search("死亡证明怎么办", top_k=5)
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: dict[str, str] = {}  # id -> 原文（保序）
        self._corpus: list[list[str]] = []  # 平行数组：分词后文档
        self._ids: list[str] = []  # 平行数组：文档 id
        self._index: BM25Okapi | None = None
        self._dirty = True

    # -- 索引维护 ---------------------------------------------------------

    def add(self, doc_id: str, text: str) -> None:
        words = tokenize_words(text)
        self._docs[doc_id] = text
        if doc_id in self._ids:
            # 覆盖既有文档：替换原文与词序列
            pos = self._ids.index(doc_id)
            self._corpus[pos] = words
        else:
            self._ids.append(doc_id)
            self._corpus.append(words)
        self._dirty = True

    def remove(self, doc_id: str) -> bool:
        if doc_id not in self._docs:
            return False
        del self._docs[doc_id]
        pos = self._ids.index(doc_id)
        del self._ids[pos]
        del self._corpus[pos]
        self._dirty = True
        return True

    def add_batch(self, docs: dict[str, str]) -> None:
        for did, text in docs.items():
            self.add(did, text)

    def __len__(self) -> int:
        return len(self._ids)

    # -- 检索 -------------------------------------------------------------

    def _ensure_index(self) -> BM25Okapi | None:
        """惰性重建 BM25Okapi；空索引返回 None。"""
        if not self._ids:
            self._index = None
            self._dirty = False
            return None
        if self._index is None or self._dirty:
            self._index = BM25Okapi(self._corpus, k1=self.k1, b=self.b)
            self._dirty = False
        return self._index

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        q_terms = tokenize_words(query)
        if not q_terms:
            return []
        index = self._ensure_index()
        if index is None:
            return []
        import numpy as np

        scores = np.asarray(index.get_scores(q_terms), dtype=float)
        order = scores.argsort()[::-1]
        results: list[dict[str, Any]] = []
        for pos in order:
            s = float(scores[pos])
            if s <= 0:
                continue
            results.append(
                {
                    "id": self._ids[int(pos)],
                    "score": round(s, 4),
                    "text": self._docs[self._ids[int(pos)]],
                }
            )
            if len(results) >= top_k:
                break
        return results

    @property
    def avgdl(self) -> float:
        index = self._ensure_index()
        return float(index.avgdl) if index is not None else 0.0
