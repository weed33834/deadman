"""P7.1 Web 中间件测试 - 限流/CSP/安全头/CORS。"""

from __future__ import annotations


import pytest

from deadman.infrastructure.rate_limiter import (
    RateLimitConfig,
    RateLimiter,
    TokenBucket,
)
from deadman.infrastructure.web_middleware import (
    CORSMiddleware,
    MiddlewareChain,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    build_default_middleware_chain,
)


@pytest.fixture(autouse=True)
def enable_web_middleware(monkeypatch):
    """启用 web_middleware feature flag。"""
    monkeypatch.setenv("DEADMAN_WEB_MIDDLEWARE_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


# =====================================================================
# TokenBucket
# =====================================================================

class TestTokenBucket:
    def test_initial_full_capacity(self):
        bucket = TokenBucket(rate_per_second=10, capacity=100)
        # 桶初始是满的,可以获取 100 个令牌
        for _ in range(100):
            assert bucket.acquire() is True
        # 第 101 个应该被拒
        assert bucket.acquire() is False

    def test_refill_after_time(self):
        # 用可注入的 clock
        clock_time = [1000.0]
        bucket = TokenBucket(rate_per_second=10, capacity=10, clock=lambda: clock_time[0])
        # 用完所有令牌
        for _ in range(10):
            assert bucket.acquire() is True
        assert bucket.acquire() is False
        # 前进 1 秒 → 补 10 个
        clock_time[0] += 1.0
        assert bucket.acquire() is True

    def test_burst_capacity(self):
        bucket = TokenBucket(rate_per_second=1, capacity=5)
        # 突发可以拿到 5 个(容量),第 6 个被拒
        for _ in range(5):
            assert bucket.acquire() is True
        assert bucket.acquire() is False


# =====================================================================
# RateLimiter
# =====================================================================

class TestRateLimiter:
    def test_different_keys_independent(self):
        limiter = RateLimiter(RateLimitConfig(rate_per_minute=10, burst=5))
        # 不同 key 各自独立
        for _ in range(5):
            assert limiter.acquire("ip:1.1.1.1") is True
        # 1.1.1.1 已用完
        assert limiter.acquire("ip:1.1.1.1") is False
        # 2.2.2.2 仍可用
        assert limiter.acquire("ip:2.2.2.2") is True

    def test_same_key_share_quota(self):
        limiter = RateLimiter(RateLimitConfig(rate_per_minute=10, burst=3))
        for _ in range(3):
            assert limiter.acquire("user:u1") is True
        assert limiter.acquire("user:u1") is False

    def test_get_state(self):
        limiter = RateLimiter(RateLimitConfig(rate_per_minute=60, burst=10))
        limiter.acquire("ip:1.1.1.1")
        state = limiter.get_state("ip:1.1.1.1")
        assert state is not None
        assert state["key"] == "ip:1.1.1.1"
        assert state["tokens_available"] <= 10

    def test_reset_clears_state(self):
        limiter = RateLimiter(RateLimitConfig(rate_per_minute=10, burst=5))
        for _ in range(5):
            limiter.acquire("ip:1.1.1.1")
        assert limiter.acquire("ip:1.1.1.1") is False
        limiter.reset("ip:1.1.1.1")
        assert limiter.acquire("ip:1.1.1.1") is True

    def test_lru_eviction(self):
        """max_buckets 上限触发 LRU 淘汰。"""
        limiter = RateLimiter(RateLimitConfig(rate_per_minute=100), max_buckets=3)
        limiter.acquire("ip:1")
        limiter.acquire("ip:2")
        limiter.acquire("ip:3")
        # 第 4 个 key 触发淘汰
        limiter.acquire("ip:4")
        assert len(limiter.list_keys()) <= 3


# =====================================================================
# RateLimitMiddleware
# =====================================================================

class TestRateLimitMiddleware:
    def test_exempt_paths_bypass(self):
        mw = RateLimitMiddleware(
            config=RateLimitConfig(rate_per_minute=1, burst=1),
            exempt_paths={"/api/health"},
        )
        # 健康检查不限流
        for _ in range(10):
            assert mw("GET", "/api/health", {}, b"", "1.1.1.1") is None

    def test_rate_limit_returns_429(self):
        mw = RateLimitMiddleware(config=RateLimitConfig(rate_per_minute=1, burst=1))
        # 第 1 个放行
        assert mw("GET", "/api/chat", {}, b"", "1.1.1.1") is None
        # 第 2 个被限流
        result = mw("GET", "/api/chat", {}, b"", "1.1.1.1")
        assert result is not None
        assert result.status == 429
        assert "rate_limited" in result.reason

    def test_retry_after_header(self):
        mw = RateLimitMiddleware(config=RateLimitConfig(rate_per_minute=1, burst=1))
        mw("GET", "/api/chat", {}, b"", "1.1.1.1")
        result = mw("GET", "/api/chat", {}, b"", "1.1.1.1")
        assert "Retry-After" in result.headers


# =====================================================================
# SecurityHeadersMiddleware
# =====================================================================

class TestSecurityHeadersMiddleware:
    def test_inject_csp_header(self):
        mw = SecurityHeadersMiddleware()
        headers = mw.inject_response_headers({})
        assert "Content-Security-Policy" in headers
        assert "X-Frame-Options" in headers
        assert headers["X-Frame-Options"] == "DENY"
        assert "X-Content-Type-Options" in headers
        assert "Strict-Transport-Security" in headers

    def test_does_not_override_existing_headers(self):
        mw = SecurityHeadersMiddleware()
        # 已有 CSP 不应被覆盖
        existing = {"Content-Security-Policy": "custom-csp"}
        result = mw.inject_response_headers(existing)
        assert result["Content-Security-Policy"] == "custom-csp"

    def test_request_passes_through(self):
        """SecurityHeaders 不拦截请求,只注入响应头。"""
        mw = SecurityHeadersMiddleware()
        result = mw("GET", "/api/anything", {}, b"", "1.1.1.1")
        assert result is None


# =====================================================================
# CORSMiddleware
# =====================================================================

class TestCORSMiddleware:
    def test_options_preflight_returns_204(self):
        mw = CORSMiddleware(allowed_origins=["https://example.com"])
        result = mw("OPTIONS", "/api/chat", {"origin": "https://example.com"}, b"", "1.1.1.1")
        assert result is not None
        assert result.status == 204
        assert result.headers["Access-Control-Allow-Origin"] == "https://example.com"

    def test_non_allowed_origin_passes_through(self):
        """不在白名单的 origin 不返回 CORS 头(浏览器同源策略生效)。"""
        mw = CORSMiddleware(allowed_origins=["https://example.com"])
        result = mw("OPTIONS", "/api/chat", {"origin": "https://evil.com"}, b"", "1.1.1.1")
        assert result is None

    def test_same_origin_no_origin_header(self):
        """同源请求无 Origin header → 不处理。"""
        mw = CORSMiddleware(allowed_origins=["https://example.com"])
        result = mw("GET", "/api/chat", {}, b"", "1.1.1.1")
        assert result is None

    def test_get_request_passes_through(self):
        """非 OPTIONS 的请求放行,响应头由 inject_response_headers 注入。"""
        mw = CORSMiddleware(allowed_origins=["https://example.com"])
        result = mw("GET", "/api/chat", {"origin": "https://example.com"}, b"", "1.1.1.1")
        assert result is None

    def test_inject_response_headers(self):
        mw = CORSMiddleware(allowed_origins=["https://example.com"])
        headers = mw.inject_response_headers("https://example.com", {})
        assert "Access-Control-Allow-Origin" in headers


# =====================================================================
# RequestSizeLimitMiddleware
# =====================================================================

class TestRequestSizeLimitMiddleware:
    def test_small_body_passes(self):
        mw = RequestSizeLimitMiddleware(max_body_bytes=1024)
        assert mw("POST", "/api/chat", {}, b"hello", "1.1.1.1") is None

    def test_large_body_blocked(self):
        mw = RequestSizeLimitMiddleware(max_body_bytes=100)
        big_body = b"x" * 200
        result = mw("POST", "/api/chat", {}, big_body, "1.1.1.1")
        assert result is not None
        assert result.status == 413
        assert "body_too_large" in result.reason

    def test_no_body_passes(self):
        mw = RequestSizeLimitMiddleware(max_body_bytes=100)
        assert mw("GET", "/api/health", {}, b"", "1.1.1.1") is None


# =====================================================================
# MiddlewareChain
# =====================================================================

class TestMiddlewareChain:
    def test_chain_passes_when_all_pass(self):
        chain = MiddlewareChain()
        chain.add(RequestSizeLimitMiddleware(max_body_bytes=1024))
        result = chain.run("GET", "/api/health", {}, b"hello", "1.1.1.1")
        assert result is None

    def test_chain_short_circuits_on_first_block(self):
        chain = MiddlewareChain()
        chain.add(RequestSizeLimitMiddleware(max_body_bytes=10))
        chain.add(RateLimitMiddleware(config=RateLimitConfig(rate_per_minute=100)))

        big_body = b"x" * 100
        result = chain.run("POST", "/api/chat", {}, big_body, "1.1.1.1")
        # 应被 size limit 拦截,不进入 rate limit
        assert result is not None
        assert result.status == 413

    def test_chain_exception_falls_through(self):
        """中间件抛异常不应阻塞请求(失败安全)。"""

        def failing_mw(method, path, headers, body, ip):
            raise RuntimeError("oops")

        def passing_mw(method, path, headers, body, ip):
            return None

        chain = MiddlewareChain()
        chain.add(failing_mw)
        chain.add(passing_mw)
        result = chain.run("GET", "/api/health", {}, b"", "1.1.1.1")
        assert result is None  # 放行

    def test_disabled_flag_bypasses_all(self, monkeypatch):
        """feature flag 关闭时中间件链透传。"""
        monkeypatch.setenv("DEADMAN_WEB_MIDDLEWARE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        chain = MiddlewareChain()
        chain.add(RequestSizeLimitMiddleware(max_body_bytes=10))
        # 即使超大 body 也放行(flag 关闭)
        result = chain.run("POST", "/api", {}, b"x" * 1000, "1.1.1.1")
        assert result is None


# =====================================================================
# build_default_middleware_chain
# =====================================================================

class TestDefaultChain:
    def test_returns_chain_and_injectors(self):
        chain, sec_headers, cors = build_default_middleware_chain()
        assert isinstance(chain, MiddlewareChain)
        assert isinstance(sec_headers, SecurityHeadersMiddleware)
        assert isinstance(cors, CORSMiddleware)
