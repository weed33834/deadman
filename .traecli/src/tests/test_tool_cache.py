"""P3.4 测试矩阵 - 工具结果缓存（LRU + TTL）

覆盖：
  1. 命中缓存直接返回
  2. 未命中调用真实工具
  3. TTL 过期后视为 miss
  4. LRU 满容量时淘汰最旧条目
  5. invalidate 失效缓存
  6. feature flag 关闭时不缓存

通过 monkeypatch 控制 feature flag。
"""

from __future__ import annotations

import time

import pytest
from deadman.mcp_server import cache as cache_module
from deadman.mcp_server.cache import ToolResultCache
from deadman.mcp_server.server import mcp

# =====================================================================
# fixture
# =====================================================================


@pytest.fixture
def cache_enabled(monkeypatch):
    """临时打开 TOOL_CACHE_ENABLED + TOOL_PERMISSIONS_ENABLED（缓存依赖权限判断 read-only）"""
    monkeypatch.setattr(cache_module, "TOOL_CACHE_ENABLED", True)
    from deadman.mcp_server import permissions as perm_module

    monkeypatch.setattr(perm_module, "TOOL_PERMISSIONS_ENABLED", True)
    # 清空全局单例避免污染
    old = cache_module._global_cache
    cache_module._global_cache = None
    # 同步 server 的 feature flag
    from deadman.mcp_server import server as srv

    monkeypatch.setattr(srv, "TOOL_CACHE_ENABLED", True)
    # 重置 server 单例的 _cache
    mcp._cache = None
    yield
    cache_module._global_cache = old
    mcp._cache = None


@pytest.fixture
def cache_disabled(monkeypatch):
    """显式关闭 TOOL_CACHE_ENABLED"""
    monkeypatch.setattr(cache_module, "TOOL_CACHE_ENABLED", False)
    from deadman.mcp_server import server as srv

    monkeypatch.setattr(srv, "TOOL_CACHE_ENABLED", False)
    mcp._cache = None
    yield


# =====================================================================
# ToolResultCache 单元测试
# =====================================================================


class TestCacheHitMiss:
    def test_cache_hit_returns_cached(self):
        """命中缓存应返回缓存结果"""
        cache = ToolResultCache(max_entries=10, default_ttl=60, enabled=True)
        args_hash = ToolResultCache.hash_args({"country": "CN"})
        cache.put("query_knowledge", args_hash, {"ok": True, "data": "cached"})
        result = cache.get("query_knowledge", args_hash)
        assert result == {"ok": True, "data": "cached"}
        assert cache.stats()["hits"] == 1
        assert cache.stats()["misses"] == 0

    def test_cache_miss_calls_tool(self):
        """未命中应返回 None"""
        cache = ToolResultCache(enabled=True)
        result = cache.get("query_knowledge", ToolResultCache.hash_args({"x": 1}))
        assert result is None
        assert cache.stats()["misses"] == 1

    def test_cache_different_args_different_entries(self):
        """不同 args_hash 应视为不同条目"""
        cache = ToolResultCache(enabled=True)
        h1 = ToolResultCache.hash_args({"country": "CN"})
        h2 = ToolResultCache.hash_args({"country": "US"})
        cache.put("query_knowledge", h1, {"data": "CN"})
        cache.put("query_knowledge", h2, {"data": "US"})
        assert cache.get("query_knowledge", h1) == {"data": "CN"}
        assert cache.get("query_knowledge", h2) == {"data": "US"}


class TestCacheTTL:
    def test_cache_ttl_expires(self):
        """TTL 过期后应视为 miss"""
        cache = ToolResultCache(default_ttl=60, enabled=True)
        args_hash = ToolResultCache.hash_args({"x": 1})
        cache.put("tool", args_hash, "data", ttl_seconds=0)
        # ttl=0 应立即过期
        # 注意：put 时已开始倒计时，get 时已过期
        # 等待极短时间确保 monotonic 推进
        time.sleep(0.01)
        result = cache.get("tool", args_hash)
        assert result is None

    def test_cache_ttl_default_uses_constructor(self):
        """未传 ttl 时用构造函数默认值"""
        cache = ToolResultCache(default_ttl=60, enabled=True)
        cache.put("tool", "h1", "data")
        # 立即 get 应命中
        assert cache.get("tool", "h1") == "data"


class TestCacheLRU:
    def test_cache_lru_eviction(self):
        """超过 max_entries 应淘汰最久未访问"""
        cache = ToolResultCache(max_entries=3, default_ttl=600, enabled=True)
        # 放 4 个，应淘汰第 1 个
        for i in range(4):
            cache.put("tool", f"h{i}", f"data{i}")
        # h0 应被淘汰
        assert cache.get("tool", "h0") is None
        # h1-h3 应在
        for i in range(1, 4):
            assert cache.get("tool", f"h{i}") == f"data{i}"
        assert cache.stats()["evictions"] >= 1

    def test_cache_lru_access_refreshes_order(self):
        """访问应刷新 LRU 顺序，避免被淘汰"""
        cache = ToolResultCache(max_entries=2, default_ttl=600, enabled=True)
        cache.put("tool", "h1", "d1")
        cache.put("tool", "h2", "d2")
        # 访问 h1，使其成为最新
        cache.get("tool", "h1")
        # 放 h3，应淘汰 h2（最旧）而非 h1
        cache.put("tool", "h3", "d3")
        assert cache.get("tool", "h1") == "d1"  # 仍在
        assert cache.get("tool", "h2") is None  # 被淘汰
        assert cache.get("tool", "h3") == "d3"


class TestCacheInvalidate:
    def test_cache_invalidate_single(self):
        """invalidate 单条应只删该条"""
        cache = ToolResultCache(enabled=True)
        cache.put("tool", "h1", "d1")
        cache.put("tool", "h2", "d2")
        n = cache.invalidate("tool", "h1")
        assert n == 1
        assert cache.get("tool", "h1") is None
        assert cache.get("tool", "h2") == "d2"

    def test_cache_invalidate_all_for_tool(self):
        """invalidate 整个工具应删全部条目"""
        cache = ToolResultCache(enabled=True)
        cache.put("tool_a", "h1", "d1")
        cache.put("tool_a", "h2", "d2")
        cache.put("tool_b", "h3", "d3")
        n = cache.invalidate("tool_a")
        assert n == 2
        assert cache.get("tool_a", "h1") is None
        assert cache.get("tool_a", "h2") is None
        assert cache.get("tool_b", "h3") == "d3"  # 其他工具不受影响

    def test_cache_clear(self):
        """clear 应清空全部"""
        cache = ToolResultCache(enabled=True)
        cache.put("t", "h", "d")
        cache.clear()
        assert cache.get("t", "h") is None
        assert cache.stats()["entries"] == 0


class TestCacheHashArgs:
    def test_hash_args_stable(self):
        """相同 args（不同字段顺序）应产生相同 hash"""
        h1 = ToolResultCache.hash_args({"a": 1, "b": 2})
        h2 = ToolResultCache.hash_args({"b": 2, "a": 1})
        assert h1 == h2

    def test_hash_args_empty(self):
        """空 args 应有固定 hash"""
        h = ToolResultCache.hash_args({})
        assert isinstance(h, str)
        assert len(h) == 64


# =====================================================================
# 集成测试：通过 mcp.call_tool 验证缓存
# =====================================================================


class TestCacheIntegration:
    async def test_cache_hit_via_call_tool(self, cache_enabled):
        """call_tool 第二次调用 read-only 工具应命中缓存"""
        # 第一次调用：未命中
        r1 = await mcp.call_tool("query_knowledge", {"country": "XX", "topic": "x"})
        assert isinstance(r1, dict)
        # 第二次相同 args：应命中缓存
        r2 = await mcp.call_tool("query_knowledge", {"country": "XX", "topic": "x"})
        assert r2.get("cache_hit") is True

    async def test_cache_not_used_for_write_tools(self, cache_enabled, tmp_path, monkeypatch):
        """write 工具不应被缓存"""
        from deadman.config import Settings

        monkeypatch.setattr("deadman.mcp_server.server.settings", Settings(project_root=tmp_path))
        r1 = await mcp.call_tool(
            "write_file",
            {"path": "data/x.txt", "content": "a", "overwrite": True, "create_dirs": True},
        )
        assert r1.get("cache_hit") is not True
        r2 = await mcp.call_tool(
            "write_file",
            {"path": "data/x.txt", "content": "a", "overwrite": True, "create_dirs": True},
        )
        # write_file 不应被缓存
        assert r2.get("cache_hit") is not True

    async def test_cache_disabled_passthrough(self, cache_disabled):
        """缓存关闭时多次调用应每次都执行（无 cache_hit 标记）"""
        r1 = await mcp.call_tool("query_knowledge", {"country": "XX", "topic": "x"})
        r2 = await mcp.call_tool("query_knowledge", {"country": "XX", "topic": "x"})
        assert r1.get("cache_hit") is not True
        assert r2.get("cache_hit") is not True
