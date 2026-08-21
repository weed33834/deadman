"""基于 IP 的速率限制器 —— 薄封装 limits 库（滑动窗口策略）。

设计要点
--------
* 每个客户端 IP 在 ``window`` 秒的滑动窗口内最多允许 ``max_requests`` 次请求。
* 内部算法由成熟库 ``limits``（MovingWindowRateLimiter + MemoryStorage）承担，
  本模块仅保留配置读取、键规整与对外 API（check/reset/current_count）。
* 超出额度时 :meth:`RateLimiter.check` 返回 ``(False, retry_after)``，
  调用方据此返回 ``429 Too Many Requests`` 并附带 ``Retry-After`` 头。
* 限额可通过环境变量 ``DEADMAN_RATE_LIMIT_MAX`` / ``DEADMAN_RATE_LIMIT_WINDOW``
  调整；健康检查等放行端点应由调用方在调用 :meth:`check` 前自行判断。

注意：本模块与 ``deadman.infrastructure.rate_limiter``（令牌桶，供 MCP 网关 /
中间件链使用）相互独立，二者职责不同，互不影响。
"""

from __future__ import annotations

import math
import os
import time

from limits import RateLimitItemPerSecond, strategies
from limits.storage import MemoryStorage

__all__ = ["RateLimiter"]


class RateLimiter:
    """基于客户端 IP 的滑动窗口速率限制器（limits MovingWindow 封装）。

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
        # limits 的 multiples 为整秒窗口；非整秒向上取整，保证至少 1 秒
        window_secs = max(1, math.ceil(window))
        self._item = RateLimitItemPerSecond(max_requests, window_secs)
        self._limiter = strategies.MovingWindowRateLimiter(MemoryStorage())

    def _key(self, ip: str) -> str:
        return f"ip:{ip or 'unknown'}"

    def check(self, ip: str) -> tuple[bool, int]:
        """检查 ``ip`` 是否被允许发起本次请求。

        Returns
        -------
        (allowed, retry_after)
            ``allowed`` 为 ``True`` 表示放行（本次请求已计入计数）；
            为 ``False`` 表示被限流，``retry_after`` 为建议客户端等待的秒数
            （至少为 1，可直接用于 ``Retry-After`` 响应头）。
        """
        key = self._key(ip)
        if self._limiter.hit(self._item, key):
            return True, 0
        stats = self._limiter.get_window_stats(self._item, key)
        retry_after = max(1.0, float(stats.reset_time - time.time()))
        return False, int(retry_after) + (1 if retry_after % 1 else 0)

    def reset(self, ip: str | None = None) -> None:
        """重置限流状态。

        传入 ``ip`` 时仅清除该 IP 的计数；为 ``None`` 时清空全部计数。
        主要用于测试。
        """
        storage = self._limiter.storage
        if ip is None:
            storage.reset()
        else:
            # 滑动窗口事件以 item.key_for(...) 全键存储于 MemoryStorage.events
            storage.events.pop(self._item.key_for(self._key(ip)), None)

    def current_count(self, ip: str) -> int:
        """返回 ``ip`` 在当前滑动窗口内已计入的请求数（主要用于观测/测试）。"""
        stats = self._limiter.get_window_stats(self._item, self._key(ip))
        return int(self.max_requests - stats.remaining)
