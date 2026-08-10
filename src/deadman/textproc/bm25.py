"""BM25 检索 —— deep-spec 20 D.1。

关键词侧标配 BM25（自实现，零依赖）。中文文档需先分词后索引。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .tokenize import tokenize_words


@dataclass
class Bm25Index:
    """BM25 索引（经典 Robertson-Sparck Jones 变体）。

    用法::

        idx = Bm25Index()
        idx.add("doc1", "身后事办理流程说明")
        idx.add("doc2", "死亡证明办理需要哪些材料")
        results = idx.search("死亡证明怎么办", top_k=5)
    """

    k1: float = 1.5
    b: float = 0.75
    _docs: dict[str, str] = field(default_factory=dict)  # id -> 原文
    _terms: dict[str, dict[str, int]] = field(default_factory=dict)  # term -> {docid: tf}
    _doc_len: dict[str, int] = field(default_factory=dict)
    _avgdl: float = 0.0

    def add(self, doc_id: str, text: str) -> None:
        words = tokenize_words(text)
        self._docs[doc_id] = text
        tf: dict[str, int] = {}
        for w in words:
            tf[w] = tf.get(w, 0) + 1
        self._terms.update({w: self._terms.get(w, {}) for w in tf})
        for w, c in tf.items():
            self._terms[w][doc_id] = c
        self._doc_len[doc_id] = len(words)
        total = sum(self._doc_len.values())
        n = len(self._doc_len) or 1
        self._avgdl = total / n

    def remove(self, doc_id: str) -> bool:
        if doc_id not in self._docs:
            return False
        del self._docs[doc_id]
        for term, posting in list(self._terms.items()):
            posting.pop(doc_id, None)
            if not posting:
                del self._terms[term]
        self._doc_len.pop(doc_id, None)
        total = sum(self._doc_len.values())
        n = len(self._doc_len) or 1
        self._avgdl = total / n
        return True

    def add_batch(self, docs: dict[str, str]) -> None:
        for did, text in docs.items():
            self.add(did, text)

    def __len__(self) -> int:
        return len(self._docs)

    def _score(self, query_terms: list[str], doc_id: str) -> float:
        dl = self._doc_len.get(doc_id, 0)
        n = len(self._doc_len) or 1
        score = 0.0
        for term in set(query_terms):
            posting = self._terms.get(term, {})
            if doc_id not in posting:
                continue
            tf = posting[doc_id]
            df = len(posting)
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            denom = tf + self.k1 * (1 - self.b + self.b * (dl / (self.avgdl if self.avgdl else 1)))
            score += idf * ((tf * (self.k1 + 1)) / denom)
        return score

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        q_terms = tokenize_words(query)
        if not q_terms:
            return []
        scored = [(self._score(q_terms, did), did) for did in self._docs]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": did, "score": round(s, 4), "text": self._docs[did]}
            for s, did in scored[:top_k]
            if s > 0
        ]

    @property
    def avgdl(self) -> float:
        return self._avgdl
