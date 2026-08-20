"""语义缓存（P1）：对近义查询命中缓存，降低重复 LLM/检索成本。

- 键：规范化查询（小写 + 去空白 + 可选字符级归一）。
- 命中判定：精确匹配优先；可选 jaccard 近义（TokenSetRatio），阈值可配。
- TTL 过期自动淘汰；LRU 容量上限。
- 线程安全（RLock）。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

__all__ = ["SemanticCache"]


class SemanticCache:
    """LRU + TTL 语义缓存（线程安全）。

    Args:
        max_entries: 最大条目数（LRU 淘汰）。
        ttl_seconds: 默认 TTL（秒）；0 表示不过期。
        similarity_threshold: 近义命中阈值（0-1，jaccard）；1.0 表示仅精确/去空白命中。
    """

    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: int = 3600,
        similarity_threshold: float = 0.85,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl = max(0, ttl_seconds)
        self.threshold = similarity_threshold
        self._lock = threading.RLock()
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()  # key -> (expires_at, value)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _normalize(text: str) -> str:
        import re

        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def _tokens(self, text: str) -> set[str]:
        from ..textproc.tokenize import tokenize_words

        return set(tokenize_words(text))

    def get(self, query: str) -> Any | None:
        """命中返回缓存值；未命中/过期返回 None。"""
        q = self._normalize(query)
        if not q:
            return None
        now = time.time()
        with self._lock:
            # 1) 精确（去空白小写）命中
            exact = self._store.get(q)
            if exact is not None:
                expires, value = exact
                if self.ttl and expires < now:
                    self._store.pop(q, None)
                    self.misses += 1
                    return None
                self._store.move_to_end(q)
                self.hits += 1
                return value
            # 2) 近义命中（jaccard 词集合）
            if self.threshold < 1.0:
                q_tokens = self._tokens(q)
                best_key: str | None = None
                best_sim = 0.0
                for key, (expires, _v) in self._store.items():
                    if self.ttl and expires < now:
                        continue
                    if not q_tokens or not self._tokens(key):
                        continue
                    sim = len(q_tokens & self._tokens(key)) / len(q_tokens | self._tokens(key))
                    if sim >= self.threshold and sim > best_sim:
                        best_sim = sim
                        best_key = key
                if best_key is not None:
                    entry = self._store[best_key]
                    self._store.move_to_end(best_key)
                    self.hits += 1
                    return entry[1]
            self.misses += 1
            return None

    def put(self, query: str, value: Any, ttl_seconds: int | None = None) -> None:
        q = self._normalize(query)
        if not q:
            return
        ttl = self.ttl if ttl_seconds is None else max(0, ttl_seconds)
        expires = (time.time() + ttl) if ttl else 0.0
        with self._lock:
            if q in self._store:
                self._store.pop(q)
            self._store[q] = (expires, value)
            self._store.move_to_end(q)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = self.misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"entries": len(self._store), "hits": self.hits, "misses": self.misses}
