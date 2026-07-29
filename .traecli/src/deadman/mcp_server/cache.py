"""P3.4 工具结果缓存 - LRU + TTL 双策略

仅缓存 READ_ONLY 工具的结果（参考 P3.3 permissions.is_read_only），
避免缓存有副作用的工具（write_file / execute_code / execute_reflexion 等）。

缓存键：(tool_name, args_hash)
  args_hash = sha256(json.dumps(args, sort_keys=True, default=str)).hexdigest()

LRU 容量默认 1000，TTL 默认 3600 秒（1 小时）。

Feature flag:DEADMAN_TOOL_CACHE_ENABLED=0（默认关闭）
关闭时 get / put 一律 no-op，调用层应自行判断是否走缓存（见 server.call_tool）。

降级路径：
  - get 命中但已过期 → 视为 miss，自动淘汰
  - put 失败（罕见，仅在 hash 不可序列化时）→ 静默忽略，不阻断调用
  - LRU 满时淘汰最久未访问的条目
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any

# =====================================================================
# 配置（feature flag，默认关闭）
# =====================================================================

TOOL_CACHE_ENABLED: bool = os.environ.get(
    "DEADMAN_TOOL_CACHE_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# LRU 最大条目数
TOOL_CACHE_MAX_ENTRIES: int = int(
    os.environ.get("DEADMAN_TOOL_CACHE_MAX_ENTRIES", "1000")
)

# 默认 TTL（秒）
TOOL_CACHE_DEFAULT_TTL: int = int(
    os.environ.get("DEADMAN_TOOL_CACHE_DEFAULT_TTL", "3600")
)


# =====================================================================
# 缓存条目
# =====================================================================


class _CacheEntry:
    """单个缓存条目"""

    __slots__ = ("result", "expires_at", "inserted_at")

    def __init__(self, result: Any, ttl_seconds: int) -> None:
        self.result = result
        self.inserted_at = time.monotonic()
        self.expires_at = self.inserted_at + max(0, ttl_seconds)

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return now >= self.expires_at


# =====================================================================
# ToolResultCache
# =====================================================================


class ToolResultCache:
    """LRU + TTL 工具结果缓存（线程安全）

    用法：
        cache = ToolResultCache(max_entries=1000, default_ttl=3600)
        cached = cache.get("query_knowledge", args_hash)
        if cached is not None:
            return cached
        result = await tool(...)
        cache.put("query_knowledge", args_hash, result)

    注意：调用方负责判断工具是否 READ_ONLY（参考 permissions.is_read_only），
    本类不做权限校验，避免循环依赖。
    """

    def __init__(
        self,
        max_entries: int = TOOL_CACHE_MAX_ENTRIES,
        default_ttl: int = TOOL_CACHE_DEFAULT_TTL,
        enabled: bool | None = None,
    ) -> None:
        """初始化缓存

        Args:
            max_entries: LRU 最大条目数
            default_ttl: 默认 TTL（秒）
            enabled: 是否启用；None 表示用模块级 TOOL_CACHE_ENABLED（默认随 feature flag）
                     显式传 True 可在 feature flag 关闭时强制启用（单元测试用）
        """
        self._store: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._max_entries = max(1, max_entries)
        self._default_ttl = max(0, default_ttl)
        self._enabled = TOOL_CACHE_ENABLED if enabled is None else bool(enabled)
        self._lock = threading.RLock()
        # 统计（仅供 observability，非关键路径）
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    # ---------- 公开接口 ----------

    def get(self, tool_name: str, args_hash: str) -> Any | None:
        """命中返回缓存结果（并刷新 LRU 顺序），未命中/过期返回 None"""
        if not self._enabled:
            return None
        key = (tool_name, args_hash)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.is_expired():
                # 过期：淘汰后视为 miss
                self._store.pop(key, None)
                self.misses += 1
                self.evictions += 1
                return None
            # 命中：刷新 LRU 顺序
            self._store.move_to_end(key)
            self.hits += 1
            return entry.result

    def put(
        self,
        tool_name: str,
        args_hash: str,
        result: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """写入缓存；超过 max_entries 时按 LRU 淘汰最旧条目"""
        if not self._enabled:
            return
        ttl = self._default_ttl if ttl_seconds is None else max(0, ttl_seconds)
        key = (tool_name, args_hash)
        with self._lock:
            # 已存在：先 pop 再 put，确保 LRU 顺序更新
            if key in self._store:
                self._store.pop(key)
            self._store[key] = _CacheEntry(result, ttl)
            self._store.move_to_end(key)
            # LRU 淘汰
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)  # 弹出最旧
                self.evictions += 1

    def invalidate(self, tool_name: str, args_hash: str | None = None) -> int:
        """失效缓存条目

        - args_hash=None：失效该工具的所有缓存条目，返回失效条数
        - 否则只失效 (tool_name, args_hash) 一条
        """
        with self._lock:
            if args_hash is not None:
                key = (tool_name, args_hash)
                if key in self._store:
                    self._store.pop(key, None)
                    return 1
                return 0
            # 失效该工具全部
            keys_to_remove = [k for k in self._store if k[0] == tool_name]
            for k in keys_to_remove:
                self._store.pop(k, None)
            return len(keys_to_remove)

    def clear(self) -> None:
        """清空全部缓存"""
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0
            self.evictions = 0

    def stats(self) -> dict[str, int]:
        """返回缓存统计快照"""
        with self._lock:
            return {
                "entries": len(self._store),
                "max_entries": self._max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }

    # ---------- 便捷方法 ----------

    @staticmethod
    def hash_args(args: dict[str, Any] | None) -> str:
        """计算 args 的稳定哈希

        用 sha256(json.dumps(args, sort_keys=True, default=str))。
        - sort_keys=True 保证字段顺序无关
        - default=str 兜底不可序列化对象（如 dataclass / datetime）
        """
        if not args:
            return "0" * 64  # 空 args 用全零占位，避免与 None 冲突
        try:
            payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # 极端降级：用 repr
            payload = repr(args)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# =====================================================================
# 全局单例（供 server.call_tool 复用）
# =====================================================================

_global_cache: ToolResultCache | None = None


def get_global_cache() -> ToolResultCache:
    """返回进程级 ToolResultCache 单例"""
    global _global_cache
    if _global_cache is None:
        _global_cache = ToolResultCache()
    return _global_cache
