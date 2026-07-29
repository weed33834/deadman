"""D5:缓存击穿 / 穿透 / 雪崩防护。

问题:
    - 缓存击穿(stampede):大量请求同时 miss 同一 key,并发回源 → DB 雪崩
    - 缓存穿透(penetration):查询不存在的 key,每次都回源 → 被 attacker 利用
    - 缓存雪崩(avalanche):大量 key 同时过期 → 同时回源

缓解:
    - Singleflight:同一 key 同时只有一个请求回源,其他等结果
    - 空值缓存:不存在的 key 也缓存空值(短 TTL,防穿透)
    - 随机过期:在 TTL 基础上加随机偏移(防雪崩)

集成:
    tool_cache.py / mcp_server/cache.py 升级:
        cache = get_cache_protection()
        result = await cache.get_or_load(
            key="tool:web_search:query_xxx",
            loader=lambda: web_search("xxx"),
            ttl=300,
        )

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar
from collections.abc import Awaitable, Callable

from ..feature_flags import is_enabled

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class SingleflightResult:
    """singleflight 结果。"""

    value: Any
    is_leader: bool  # 是否是发起请求的那个
    waiters: int = 0  # 等了多少个其他请求


@dataclass
class CacheEntry:
    """缓存条目。"""

    value: Any
    expires_at: float
    is_null: bool = False  # 是否空值缓存(防穿透)
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0


@dataclass
class CacheStats:
    """缓存统计。"""

    hits: int = 0
    misses: int = 0
    null_hits: int = 0  # 空值命中(防穿透命中)
    stampede_prevented: int = 0  # singleflight 防止的击穿数
    evictions: int = 0
    total_keys: int = 0

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class CacheProtection:
    """缓存保护器(singleflight + 防穿透 + 防雪崩)。

    用法(同步):
        cache = get_cache_protection()
        result = cache.get_or_load_sync(
            key="tool:web_search:q1",
            loader=lambda: web_search("q1"),
            ttl=300,
        )

    用法(异步):
        result = await cache.get_or_load(
            key="tool:web_search:q1",
            loader=lambda: asyncio.to_thread(web_search, "q1"),
            ttl=300,
        )
    """

    def __init__(
        self,
        # 默认 TTL(秒)
        default_ttl: int = 300,
        # 空值缓存 TTL(秒,防穿透)
        null_ttl: int = 30,
        # TTL 抖动范围(防雪崩):实际 TTL = ttl ± ttl * jitter
        ttl_jitter: float = 0.1,
        # 最大缓存条目数(LRU 淘汰)
        max_entries: int = 10_000,
    ) -> None:
        self.default_ttl = default_ttl
        self.null_ttl = null_ttl
        self.ttl_jitter = ttl_jitter
        self.max_entries = max_entries

        self._lock = threading.RLock()
        self._cache: dict[str, CacheEntry] = {}
        # singleflight:同一 key 同时只有一个 loader 执行
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_results: dict[str, Any] = {}
        # 异步 singleflight
        self._async_inflight: dict[str, asyncio.Future] = {}

        self._stats = CacheStats()

    def get_or_load_sync(
        self,
        key: str,
        loader: Callable[[], T],
        ttl: int | None = None,
        allow_null_cache: bool = True,
    ) -> T:
        """同步:获取或加载(带 singleflight 防击穿)。"""
        if not is_enabled("defense"):
            return loader()

        # 1. 查缓存
        entry = self._get_cached(key)
        if entry is not None:
            with self._lock:
                self._stats.hits += 1
                if entry.is_null:
                    self._stats.null_hits += 1
            return entry.value

        # 2. cache miss → singleflight
        with self._lock:
            self._stats.misses += 1
            if key in self._inflight:
                # 已有请求在执行,等待结果
                event = self._inflight[key]
                self._stats.stampede_prevented += 1
                is_leader = False
            else:
                event = threading.Event()
                self._inflight[key] = event
                is_leader = True

        if is_leader:
            # 领导者:执行 loader
            try:
                value = loader()
                self._set_cached(key, value, ttl, allow_null_cache)
                with self._lock:
                    self._inflight_results[key] = value
                return value
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                    event.set()
        else:
            # 等待者:等领导结果
            event.wait(timeout=60)  # 防死锁,60s 超时
            with self._lock:
                result = self._inflight_results.get(key)
            if result is not None:
                return result
            # 领导已超时 / 失败,自己再试一次(降级)
            return loader()

    async def get_or_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        ttl: int | None = None,
        allow_null_cache: bool = True,
    ) -> T:
        """异步:获取或加载(带 singleflight 防击穿)。"""
        if not is_enabled("defense"):
            return await loader()

        # 1. 查缓存
        entry = self._get_cached(key)
        if entry is not None:
            with self._lock:
                self._stats.hits += 1
                if entry.is_null:
                    self._stats.null_hits += 1
            return entry.value

        # 2. cache miss → singleflight
        loop = asyncio.get_event_loop()
        with self._lock:
            self._stats.misses += 1
            if key in self._async_inflight:
                future = self._async_inflight[key]
                self._stats.stampede_prevented += 1
                is_leader = False
            else:
                future = loop.create_future()
                self._async_inflight[key] = future
                is_leader = True

        if is_leader:
            try:
                value = await loader()
                self._set_cached(key, value, ttl, allow_null_cache)
                with self._lock:
                    if not future.done():
                        future.set_result(value)
                    self._async_inflight.pop(key, None)
                return value
            except Exception as e:
                with self._lock:
                    if not future.done():
                        future.set_exception(e)
                    self._async_inflight.pop(key, None)
                raise
        else:
            # 等待领导结果
            return await future

    def get(self, key: str) -> Any | None:
        """直接查缓存(不加载)。"""
        entry = self._get_cached(key)
        if entry is None:
            return None
        return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        allow_null_cache: bool = False,
    ) -> None:
        """直接写缓存。"""
        if not is_enabled("defense"):
            return
        self._set_cached(key, value, ttl, allow_null_cache)

    def invalidate(self, key: str) -> bool:
        """删除缓存。"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._stats.evictions += 1
                return True
            return False

    def clear(self) -> int:
        """清空所有缓存。"""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def stats(self) -> CacheStats:
        """获取统计(看板用)。"""
        with self._lock:
            self._stats.total_keys = len(self._cache)
            return self._stats

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_cached(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() > entry.expires_at:
                del self._cache[key]
                self._stats.evictions += 1
                return None
            entry.hit_count += 1
            return entry

    def _set_cached(
        self,
        key: str,
        value: Any,
        ttl: int | None,
        allow_null_cache: bool,
    ) -> None:
        with self._lock:
            # 空值缓存(防穿透)
            if value is None and not allow_null_cache:
                return

            actual_ttl = ttl or self.default_ttl
            # 加抖动(防雪崩)
            if self.ttl_jitter > 0:
                jitter = actual_ttl * self.ttl_jitter * (random.random() * 2 - 1)
                actual_ttl = max(1, int(actual_ttl + jitter))

            is_null = value is None
            if is_null:
                actual_ttl = min(actual_ttl, self.null_ttl)

            self._cache[key] = CacheEntry(
                value=value,
                expires_at=time.time() + actual_ttl,
                is_null=is_null,
            )

            # LRU 淘汰
            if len(self._cache) > self.max_entries:
                # 简化 LRU:删除最早的(实际应按 access time)
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                self._stats.evictions += 1

    @staticmethod
    def make_key(*parts: str) -> str:
        """生成 cache key(hash 防过长)。"""
        joined = "|".join(str(p) for p in parts)
        if len(joined) > 200:
            h = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
            return f"hashed:{h}"
        return joined


# 全局单例
_cp_instance: CacheProtection | None = None
_cp_lock = threading.Lock()


def get_cache_protection() -> CacheProtection:
    global _cp_instance
    if _cp_instance is None:
        with _cp_lock:
            if _cp_instance is None:
                _cp_instance = CacheProtection()
    return _cp_instance
