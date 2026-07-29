"""P7.1a 限流器 - 令牌桶(Token Bucket)算法。

借鉴 Stripe/Nginx 的限流实践,支持:
    - 按维度限流(IP / user_id / tenant_id / 全局)
    - 突发流量(burst)支持:桶满后允许瞬时 burst
    - 多窗口(秒/分/小时)
    - 公平性:同维度所有请求共享配额,先到先得

并发安全:加锁保护令牌桶内部状态。
集成点:
    - Web 中间件(web_middleware.py)
    - MCP 工具调用(mcp_server/gateway.py)
    - A2A 入口(a2a/server.py)

feature flag:`DEADMAN_WEB_MIDDLEWARE_ENABLED=0` 默认关闭(由调用方判断)。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """限流配置。

    Attributes:
        rate_per_second: 每秒令牌生成速率(0=不限)
        rate_per_minute: 每分钟令牌上限(0=不限)
        rate_per_hour: 每小时令牌上限(0=不限)
        burst: 桶容量(允许突发,默认 = rate_per_second * 2)
    """

    rate_per_second: int = 0
    rate_per_minute: int = 100
    rate_per_hour: int = 0
    burst: int = 0  # 0 时默认 = rate_per_second * 2

    def effective_burst(self) -> int:
        if self.burst > 0:
            return self.burst
        return max(self.rate_per_second * 2, 10)


class TokenBucket:
    """令牌桶 - 单维度限流。

    原理:
        - 桶容量 = burst
        - 每秒补充 rate_per_second 个令牌(惰性计算,基于时间差)
        - 每次 acquire(n) 消耗 n 个令牌,不够则拒绝
    """

    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        clock: callable | None = None,
    ) -> None:
        """
        Args:
            rate_per_second: 每秒补充的令牌数
            capacity: 桶容量(突发上限)
            clock: 时间函数(可注入,便于测试)
        """
        self.rate = float(rate_per_second)
        self.capacity = max(1, int(capacity))
        self._tokens = float(self.capacity)
        self._last_refill = (clock or time.time)()
        self._clock = clock or time.time
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> bool:
        """尝试获取 n 个令牌。

        Returns:
            True=成功,False=被限流
        """
        with self._lock:
            now = self._clock()
            elapsed = now - self._last_refill
            # 惰性补充令牌
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.rate,
            )
            self._last_refill = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def try_acquire(self, tokens: int = 1, max_wait_seconds: float = 0.0) -> bool:
        """带等待的尝试获取(用于背压)。"""
        if max_wait_seconds <= 0:
            return self.acquire(tokens)
        deadline = self._clock() + max_wait_seconds
        while self._clock() < deadline:
            if self.acquire(tokens):
                return True
            time.sleep(0.05)
        return False

    def peek_available(self) -> float:
        """查询当前可用令牌数(不消耗)。"""
        with self._lock:
            now = self._clock()
            elapsed = now - self._last_refill
            return min(self.capacity, self._tokens + elapsed * self.rate)

    @property
    def tokens(self) -> float:
        """兼容旧代码:暴露 tokens 字段(惰性补充)。"""
        return self.peek_available()


class RateLimiter:
    """多维度限流器。

    按 key(如 IP / user_id / tenant_id)维护独立令牌桶。
    超过 max_buckets 时按 LRU 淘汰最久未访问的桶(避免内存爆炸)。

    用法:
        limiter = RateLimiter(RateLimitConfig(rate_per_minute=100))
        if limiter.acquire("ip:1.2.3.4"):
            # 放行
        else:
            # 429 Too Many Requests
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        max_buckets: int = 10000,
        clock: callable | None = None,
    ) -> None:
        self.config = config or RateLimitConfig()
        self.max_buckets = max_buckets
        self._clock = clock or time.time
        self._lock = threading.RLock()
        # key -> (TokenBucket, last_access_time)
        self._buckets: dict[str, tuple[TokenBucket, float]] = {}

    def acquire(self, key: str, tokens: int = 1) -> bool:
        """按 key 限流。

        Returns:
            True=允许,False=被限流
        """
        with self._lock:
            # LRU 淘汰
            if len(self._buckets) >= self.max_buckets:
                self._evict_lru()

            if key not in self._buckets:
                self._buckets[key] = (
                    self._make_bucket(),
                    self._clock(),
                )

            bucket, _ = self._buckets[key]
            # 更新访问时间
            self._buckets[key] = (bucket, self._clock())

        # 实际 acquire 在锁外执行(避免长锁)
        return bucket.acquire(tokens)

    def get_state(self, key: str) -> dict | None:
        """查询某 key 的限流状态(看板用)。"""
        with self._lock:
            entry = self._buckets.get(key)
        if entry is None:
            return None
        bucket, last_access = entry
        return {
            "key": key,
            "tokens_available": bucket.peek_available(),
            "capacity": bucket.capacity,
            "rate_per_second": bucket.rate,
            "last_access": last_access,
        }

    def list_keys(self) -> list[str]:
        """列出所有 key(运维排查用)。"""
        with self._lock:
            return list(self._buckets.keys())

    def reset(self, key: str | None = None) -> None:
        """重置限流(指定 key 或全部)。"""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def _make_bucket(self) -> TokenBucket:
        """根据 config 创建新桶。"""
        # 优先级:rate_per_second > rate_per_minute/60
        if self.config.rate_per_second > 0:
            rate = self.config.rate_per_second
        elif self.config.rate_per_minute > 0:
            rate = self.config.rate_per_minute / 60.0
        elif self.config.rate_per_hour > 0:
            rate = self.config.rate_per_hour / 3600.0
        else:
            # 不限流(rate=很大)
            rate = 1_000_000.0
        return TokenBucket(
            rate_per_second=rate,
            capacity=self.config.effective_burst(),
            clock=self._clock,
        )

    def _evict_lru(self) -> None:
        """LRU 淘汰最久未访问的桶。"""
        if not self._buckets:
            return
        # 找最老的 key
        oldest_key = min(self._buckets.keys(), key=lambda k: self._buckets[k][1])
        self._buckets.pop(oldest_key, None)
