"""基于 IP 的内存滑动窗口速率限制器（纯 stdlib 实现）。

设计要点
--------
* 每个客户端 IP 在 ``window`` 秒的滑动窗口内最多允许 ``max_requests`` 次请求。
* 超出额度时 :meth:`RateLimiter.check` 返回 ``(False, retry_after)``，
  调用方据此返回 ``429 Too Many Requests`` 并附带 ``Retry-After`` 头。
* 全部状态保存在进程内存中，使用 :class:`threading.Lock` 保护，
  兼容 ``ThreadingHTTPServer`` 的多线程模型。
* 限额可通过环境变量 ``DEADMAN_RATE_LIMIT_MAX`` / ``DEADMAN_RATE_LIMIT_WINDOW``
  调整；健康检查等放行端点应由调用方在调用 :meth:`check` 前自行判断。

注意：本模块与 ``deadman.infrastructure.rate_limiter``（令牌桶，供 MCP 网关 /
中间件链使用）相互独立，二者职责不同，互不影响。
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

__all__ = ["RateLimiter"]


class RateLimiter:
    """基于客户端 IP 的滑动窗口速率限制器。

    Parameters
    ----------
    max_requests:
        单个滑动窗口内允许的最大请求数，默认 60。可通过环境变量
        ``DEADMAN_RATE_LIMIT_MAX`` 覆盖。
    window:
        滑动窗口长度（秒），默认 60。可通过环境变量
        ``DEADMAN_RATE_LIMIT_WINDOW`` 覆盖。
    """

    def __init__(
        self,
        max_requests: int | None = None,
        window: float | None = None,
    ) -> None:
        if max_requests is None:
            max_requests = int(os.getenv("DEADMAN_RATE_LIMIT_MAX", "60"))
        if window is None:
            window = float(os.getenv("DEADMAN_RATE_LIMIT_WINDOW", "60"))

        if max_requests <= 0:
            raise ValueError("max_requests 必须为正整数")
        if window <= 0:
            raise ValueError("window 必须为正数")

        self.max_requests = max_requests
        self.window = window
        # IP -> 该 IP 在窗口内各次请求的单调时间戳队列
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, ip: str) -> tuple[bool, int]:
        """检查 ``ip`` 是否被允许发起本次请求。

        线程安全。每次调用都会：清理该 IP 队列中已滑出窗口的旧时间戳，
        再判断当前窗口内请求数是否达到上限。

        Returns
        -------
        (allowed, retry_after)
            ``allowed`` 为 ``True`` 表示放行（本次请求已计入计数）；
            为 ``False`` 表示被限流，``retry_after`` 为建议客户端等待的秒数
            （至少为 1，可直接用于 ``Retry-After`` 响应头）。
        """
        if not ip:
            ip = "unknown"
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits[ip]
            # 弹出已滑出窗口的时间戳
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                # 队首时间戳 + window 即为该 IP 最早可恢复额度的时刻
                retry_after = dq[0] + self.window - now
                # 向上取整，至少 1 秒，符合 Retry-After 语义
                retry_after_int = max(1, int(retry_after) + (1 if retry_after % 1 else 0))
                return False, retry_after_int
            dq.append(now)
            return True, 0

    def reset(self, ip: str | None = None) -> None:
        """重置限流状态。

        传入 ``ip`` 时仅清除该 IP 的计数；为 ``None`` 时清空全部计数。
        主要用于测试。
        """
        with self._lock:
            if ip is None:
                self._hits.clear()
            else:
                self._hits.pop(ip, None)

    def current_count(self, ip: str) -> int:
        """返回 ``ip`` 在当前滑动窗口内已计入的请求数（主要用于观测/测试）。"""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits.get(ip)
            if not dq:
                return 0
            # 计数前先排除已过期项（不修改原队列）
            return sum(1 for ts in dq if ts > cutoff)
