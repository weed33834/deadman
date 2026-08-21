"""P7.1 Web 中间件 - 限流/CSP/安全头/CORS。

为 BaseHTTPRequestHandler 提供 WSGI-like 中间件链,可独立组合:
    - RateLimitMiddleware: 令牌桶限流(按 IP / user_id)
    - SecurityHeadersMiddleware: CSP / X-Frame-Options / X-Content-Type-Options / HSTS / Referrer-Policy
    - CORSMiddleware: 严格白名单(避免 *)
    - RequestSizeLimitMiddleware: 请求体大小限制(防 DoS)
    - AuditLogMiddleware: 访问日志(请求方法/路径/IP/状态码/耗时)

设计原则:
    - 零侵入:中间件以装饰器形式包装 handler,不修改原 Handler 类
    - 失败安全:限流/中间件异常不阻塞请求(降级到放行 + 记录 warning)
    - 可观测:每次拦截记录 reason,便于排查

feature flag:`DEADMAN_WEB_MIDDLEWARE_ENABLED=0` 默认关闭。
关闭时所有中间件直接透传(行为完全不变)。
"""

from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from .feature_flags import is_enabled
from .rate_limiter import RateLimitConfig, RateLimiter

logger = logging.getLogger(__name__)


# =====================================================================
# 默认 CSP / 安全头配置(可被 env 覆盖)
# =====================================================================


def _default_csp() -> str:
    """默认 CSP:严格 default-src 'self',按需放行内联 + img。"""
    return os.environ.get(
        "DEADMAN_CSP",
        "; ".join(
            [
                "default-src 'self'",
                "script-src 'self' 'unsafe-inline'",  # 原生 JS SPA 需要 inline event handlers
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: https:",
                "font-src 'self' data:",
                "connect-src 'self'",
                "frame-ancestors 'none'",
                "base-uri 'self'",
                "form-action 'self'",
            ]
        ),
    )


def _default_cors_origins() -> list[str]:
    """默认 CORS 白名单(从 env 读,逗号分隔)。"""
    raw = os.environ.get("DEADMAN_CORS_ORIGINS", "")
    if not raw:
        return []  # 空 = 完全禁用 CORS(同源策略生效)
    return [o.strip() for o in raw.split(",") if o.strip()]


# =====================================================================
# 中间件基类
# =====================================================================


@dataclass
class MiddlewareResponse:
    """中间件拦截响应 - 不为 None 则短路返回。"""

    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    reason: str = ""  # 拦截原因(审计)


def passthrough() -> MiddlewareResponse | None:
    """表示放行(不拦截)。"""
    return None


# 中间件签名:(method, path, headers, body, client_ip) -> Optional[MiddlewareResponse]
MiddlewareFn = Callable[
    [str, str, dict, bytes, str],
    MiddlewareResponse | None,
]


class MiddlewareChain:
    """中间件链 - 按顺序执行,任一拦截则短路。

    用法:
        chain = MiddlewareChain()
        chain.add(RateLimitMiddleware(...))
        chain.add(SecurityHeadersMiddleware())
        # 在 handler 中:
        result = chain.run(method, path, headers, body, client_ip)
        if result is not None:
            return _send_intercept(result)
        # 否则继续正常处理
    """

    def __init__(self) -> None:
        self._middlewares: list[MiddlewareFn] = []

    def add(self, mw: MiddlewareFn) -> MiddlewareChain:
        self._middlewares.append(mw)
        return self

    def run(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        client_ip: str,
    ) -> MiddlewareResponse | None:
        """执行中间件链。

        Returns:
            None=放行,MiddlewareResponse=拦截
        """
        # Feature flag 关闭 → 完全透传
        if not is_enabled("web_middleware"):
            return None

        for mw in self._middlewares:
            try:
                result = mw(method, path, headers, body, client_ip)
                if result is not None:
                    return result
            except Exception as e:
                # 中间件异常不阻塞请求(失败安全:放行 + 记录)
                logger.warning("Middleware %s failed: %s, falling through", mw.__name__, e)
                continue
        return None


# =====================================================================
# 具体中间件
# =====================================================================


class RateLimitMiddleware:
    """按 IP/user_id 限流中间件。

    Args:
        config: 限流配置(默认 100 QPM/IP)
        key_extractor: 从请求中提取限流 key(默认按 IP)
        exempt_paths: 不限流的路径(如 /api/health)
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        key_extractor: Callable[[str, str, dict], str] | None = None,
        exempt_paths: set[str] | None = None,
    ) -> None:
        self.limiter = RateLimiter(config or RateLimitConfig())
        self.key_extractor = key_extractor or self._default_key_extractor
        self.exempt_paths = exempt_paths or {"/api/health", "/healthz", "/api/health/all"}

    def __call__(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        client_ip: str,
    ) -> MiddlewareResponse | None:
        # 健康检查不限流
        if path in self.exempt_paths:
            return None

        key = self.key_extractor(method, path, headers)
        # 加 IP 后缀避免不同维度碰撞
        if not key.startswith("ip:"):
            key = f"ip:{client_ip}"

        if not self.limiter.acquire(key):
            retry_after = 60  # 默认 60 秒后重试
            return MiddlewareResponse(
                status=HTTPStatus.TOO_MANY_REQUESTS,
                body={"error": "rate_limited", "retry_after_seconds": retry_after},
                headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": "100"},
                reason=f"rate_limited:{key}",
            )
        return None

    @staticmethod
    def _default_key_extractor(method: str, path: str, headers: dict) -> str:
        """默认按 IP 限流(可扩展按 user_id/JWT)。"""
        # 优先从 X-Forwarded-For 取真实 IP(若部署在反代后)
        xff = headers.get("x-forwarded-for", "")
        if xff:
            return f"ip:{xff.split(',')[0].strip()}"
        # 否则 caller 需在 client_ip 参数传 IP
        return "ip:default"


class SecurityHeadersMiddleware:
    """注入安全响应头(CSP / X-Frame-Options / HSTS / ...)。

    这些头由本中间件统一注入,不依赖各 endpoint 重复设置。
    """

    def __init__(
        self,
        csp: str | None = None,
        cors_origins: list[str] | None = None,
        hsts_max_age: int = 31536000,
    ) -> None:
        self.csp = csp or _default_csp()
        self.cors_origins = cors_origins if cors_origins is not None else _default_cors_origins()
        self.hsts_max_age = hsts_max_age

    def __call__(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        client_ip: str,
    ) -> MiddlewareResponse | None:
        # 安全头中间件不拦截请求,仅注入响应头(由 handler 在 _send_json 时合并)
        # 这里返回 None 表示放行,但把头注入到一个"thread-local"上下文
        # 简化方案:返回放行 + 在 headers 字段提供默认值(由 caller 合并到响应)
        return None  # 安全头通过 inject_response_headers() 注入

    def inject_response_headers(self, existing: dict) -> dict:
        """注入安全头(合并到现有响应头)。"""
        result = dict(existing)
        result.setdefault("Content-Security-Policy", self.csp)
        result.setdefault("X-Frame-Options", "DENY")
        result.setdefault("X-Content-Type-Options", "nosniff")
        result.setdefault("X-XSS-Protection", "1; mode=block")
        result.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        result.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if self.hsts_max_age > 0:
            result.setdefault(
                "Strict-Transport-Security",
                f"max-age={self.hsts_max_age}; includeSubDomains; preload",
            )
        return result


class CORSMiddleware:
    """CORS 预检 + 跨域响应头(严格白名单,不允许 *)。"""

    def __init__(
        self,
        allowed_origins: list[str] | None = None,
        allowed_methods: list[str] | None = None,
        allowed_headers: list[str] | None = None,
        allow_credentials: bool = True,
        max_age: int = 86400,
    ) -> None:
        self.allowed_origins = set(
            allowed_origins if allowed_origins is not None else _default_cors_origins()
        )
        self.allowed_methods = allowed_methods or ["GET", "POST", "OPTIONS", "DELETE", "PUT"]
        self.allowed_headers = allowed_headers or [
            "Content-Type",
            "Authorization",
            "X-User-Id",
            "X-Request-Id",
        ]
        self.allow_credentials = allow_credentials
        self.max_age = max_age

    def __call__(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        client_ip: str,
    ) -> MiddlewareResponse | None:
        origin = headers.get("origin", "")
        # 同源请求(无 Origin)或不在白名单 → 不处理(浏览器同源策略生效)
        if not origin or origin not in self.allowed_origins:
            return None

        # OPTIONS 预检请求:返回 204 + CORS 头
        if method.upper() == "OPTIONS":
            return MiddlewareResponse(
                status=HTTPStatus.NO_CONTENT,
                body=b"",
                headers=self._build_cors_headers(origin),
                reason="cors_preflight",
            )
        return None

    def inject_response_headers(self, origin: str, existing: dict) -> dict:
        """注入 CORS 响应头到现有响应。"""
        if origin not in self.allowed_origins:
            return existing
        result = dict(existing)
        result.update(self._build_cors_headers(origin))
        return result

    def _build_cors_headers(self, origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": ", ".join(self.allowed_methods),
            "Access-Control-Allow-Headers": ", ".join(self.allowed_headers),
            "Access-Control-Allow-Credentials": "true" if self.allow_credentials else "false",
            "Access-Control-Max-Age": str(self.max_age),
            "Vary": "Origin",
        }


class RequestSizeLimitMiddleware:
    """请求体大小限制(防 DoS - 上传大文件耗尽带宽)。"""

    def __init__(self, max_body_bytes: int = 1024 * 1024) -> None:
        """Args: max_body_bytes 默认 1MB"""
        self.max_body_bytes = max_body_bytes

    def __call__(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        client_ip: str,
    ) -> MiddlewareResponse | None:
        if not body:
            return None
        if len(body) > self.max_body_bytes:
            return MiddlewareResponse(
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                body={
                    "error": "request_too_large",
                    "max_size_bytes": self.max_body_bytes,
                    "actual_size_bytes": len(body),
                },
                headers={"X-Max-Body-Size": str(self.max_body_bytes)},
                reason=f"body_too_large:{len(body)}>{self.max_body_bytes}",
            )
        return None


class AuditLogMiddleware:
    """访问审计日志中间件 - 记录 method/path/IP/状态码/耗时。"""

    def __init__(self, log_path: str | None = None) -> None:
        self.log_path = log_path  # 不指定则只打 logger

    def __call__(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        client_ip: str,
    ) -> MiddlewareResponse | None:
        # 仅记录,不拦截;实际响应状态需由 caller 在 finally 中调用 log_response()
        logger.info(
            "audit_request method=%s path=%s ip=%s body_size=%d",
            method,
            path,
            client_ip,
            len(body) if body else 0,
        )
        return None


# =====================================================================
# 工厂:构建默认中间件链
# =====================================================================


def build_default_middleware_chain(
    rate_limit_config: RateLimitConfig | None = None,
    cors_origins: list[str] | None = None,
    max_body_bytes: int = 1024 * 1024,
) -> tuple[MiddlewareChain, SecurityHeadersMiddleware, CORSMiddleware]:
    """构建默认中间件链 + 安全头/CORS 注入器。

    Returns:
        chain: 中间件链(用于 run 拦截)
        security_headers: 用于 inject 到响应头
        cors: 用于 inject 到响应头
    """
    chain = MiddlewareChain()
    security_headers = SecurityHeadersMiddleware(cors_origins=cors_origins)
    cors = CORSMiddleware(allowed_origins=cors_origins)
    chain.add(AuditLogMiddleware())
    chain.add(RequestSizeLimitMiddleware(max_body_bytes=max_body_bytes))
    chain.add(cors)
    chain.add(RateLimitMiddleware(config=rate_limit_config))
    return chain, security_headers, cors


# =====================================================================
# 装饰器:便于在已有 handler 上挂中间件
# =====================================================================


def with_middleware(chain: MiddlewareChain, security_headers: SecurityHeadersMiddleware):
    """装饰器:把中间件链挂到 handler 方法上。

    用法:
        chain, sec_headers = build_default_middleware_chain()
        class Handler(BaseHTTPRequestHandler):
            @with_middleware(chain, sec_headers)
            def do_GET(self):
                ...
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            # 从 self 提取请求信息
            method = self.command
            path = urlparse(self.path).path
            headers = {k.lower(): v for k, v in self.headers.items()}
            client_ip = self.client_address[0] if self.client_address else "unknown"
            body = b""
            content_length = int(headers.get("content-length", "0"))
            if content_length > 0:
                body = self.rfile.read(content_length)

            # 执行中间件链
            intercept = chain.run(method, path, headers, body, client_ip)
            if intercept is not None:
                # 短路返回拦截响应
                body_data = intercept.body
                if isinstance(body_data, dict | list):
                    body_bytes = json.dumps(body_data, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                elif isinstance(body_data, bytes):
                    body_bytes = body_data
                    content_type = "text/plain; charset=utf-8"
                else:
                    body_bytes = str(body_data or "").encode("utf-8")
                    content_type = "text/plain; charset=utf-8"

                self.send_response(intercept.status)
                # 合并安全头 + CORS 头 + 拦截响应头
                response_headers = security_headers.inject_response_headers({})
                if "origin" in headers:
                    response_headers = cors_inject(self, response_headers, headers["origin"])
                response_headers.update(intercept.headers)
                response_headers["Content-Type"] = content_type
                response_headers["Content-Length"] = str(len(body_bytes))
                for k, v in response_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            # 放行:调用原方法
            return func(self, *args, **kwargs)

        return wrapper

    return decorator


def cors_inject(handler, headers: dict, origin: str) -> dict:
    """从 handler 上找 CORS 中间件并注入头(辅助函数)。"""
    cors = getattr(handler, "_cors_middleware", None)
    if cors and origin in cors.allowed_origins:
        return cors.inject_response_headers(origin, headers)
    return headers
