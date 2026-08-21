"""FastAPI 原生中间件 + 全局异常处理 —— 企业级可观测/安全/限流。

本模块为 :mod:`deadman.web.app` 补齐 stdlib ``http.server`` 时代缺失的
企业级横切关注点，**复用** 已有组件，不重复造轮子：

* **安全响应头** —— 复用 ``infrastructure.web_middleware._default_csp`` 配置
* **限流** —— 复用 ``web.rate_limiter.RateLimiter``（纯 stdlib 滑动窗口）
* **请求日志** —— 结构化访问日志（method/path/status/耗时/IP）
* **全局异常** —— 统一 JSON 错误格式，避免栈泄露给客户端

设计原则：
* **失败安全** —— 中间件异常不阻塞请求（降级放行 + 记 warning）
* **零侵入** —— 通过 ``app.add_middleware`` / ``app.exception_handler`` 注册，
  不改动任何业务路由代码
* **可配置** —— 限流/安全头均可通过环境变量调优
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..errors import DeadmanError

logger = logging.getLogger(__name__)


# =====================================================================
# 安全响应头中间件（复用 infrastructure.web_middleware 的 CSP 配置）
# =====================================================================


def _build_security_headers() -> dict[str, str]:
    """构造安全响应头集合。

    复用 ``infrastructure.web_middleware._default_csp``（可被 env 覆盖），
    其余头按 OWASP 最佳实践硬编码。
    """
    try:
        from ..infrastructure.web_middleware import _default_csp

        csp = _default_csp()
    except Exception:  # pragma: no cover - 防御性降级
        csp = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"

    # 移动端 /m 需要被同源 iframe 预览，故 SAMEORIGIN（与 web/server.py 一致）
    return {
        "Content-Security-Policy": csp,
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
    }


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为所有响应注入安全头（CSP / HSTS / X-Frame-Options ...）。

    使用 ``BaseHTTPMiddleware`` 而非纯 ASGI，以便在响应阶段统一注入头。
    开销极小（一次 dict 合并），适合生产。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._headers = _build_security_headers()

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        # 仅在响应头未显式设置时注入（避免覆盖业务侧自定义值）
        for k, v in self._headers.items():
            response.headers.setdefault(k, v)
        return response


# =====================================================================
# 限流中间件（复用 web.rate_limiter.RateLimiter）
# =====================================================================


# 健康检查/静态资源/文档路径不限流，避免探针被掐断
_RATE_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/health",
    "/healthz",
    "/readyz",
    "/metrics",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/static/",
    "/manifest.json",
    "/sw.js",
    "/mobile.js",
)


def _client_ip(request: Request) -> str:
    """提取真实客户端 IP（优先 X-Forwarded-For，兼容反代部署）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按 IP 滑动窗口限流（复用 ``web.rate_limiter.RateLimiter``）。

    超额返回 ``429 Too Many Requests`` + ``Retry-After`` 头。
    健康检查 / 静态资源 / 文档路径放行。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # 延迟导入避免循环依赖
        from .rate_limiter import RateLimiter

        self._limiter = RateLimiter()
        self._enabled = os.getenv("DEADMAN_RATE_LIMIT_ENABLED", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not self._enabled:
            return await call_next(request)

        path = request.url.path
        if path.startswith(_RATE_EXEMPT_PREFIXES):
            return await call_next(request)

        ip = _client_ip(request)
        try:
            allowed, retry_after = self._limiter.check(ip)
        except Exception:  # pragma: no cover - 限流异常降级放行
            logger.warning("RateLimiter.check 异常，降级放行 ip=%s", ip, exc_info=True)
            return await call_next(request)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": f"请求过于频繁，请在 {retry_after} 秒后重试",
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._limiter.max_requests),
                },
            )
        return await call_next(request)


# =====================================================================
# 请求日志中间件（结构化访问日志 + 请求 ID 透传）
# =====================================================================


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """结构化访问日志：method/path/status/耗时/IP/请求ID。

    为每个请求注入 ``X-Request-ID``（若客户端未带则生成），
    便于全链路追踪。慢请求（>1s）额外 warning 级别记录。
    """

    _SLOW_THRESHOLD_MS = 1000

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        # 让下游 handler 可读到 request_id
        request.state.request_id = request_id
        # P1-2: 关联 Sentry scope，便于错误事件按 request_id 追踪
        try:
            from ..observability.sentry_init import add_request_tag

            add_request_tag("request_id", request_id)
        except Exception:
            pass

        start = time.perf_counter()
        method = request.method
        path = request.url.path

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request_unhandled method=%s path=%s ip=%s rid=%s elapsed_ms=%.1f",
                method,
                path,
                _client_ip(request),
                request_id,
                elapsed_ms,
            )
            # P1-2: 中间件层捕获的异常也上报 Sentry（兜底 handler 会再上报一次，
            # 但中间件层能捕获未走 handler 的异常，双上报由 Sentry 去重）
            try:
                from ..observability.sentry_init import capture_exception

                capture_exception(
                    request_id=request_id,
                    path=path,
                    method=method,
                    elapsed_ms=round(elapsed_ms, 2),
                )
            except Exception:
                pass
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id

        log_msg = "access method=%s path=%s status=%d ip=%s rid=%s elapsed_ms=%.1f"
        log_args = (
            method,
            path,
            response.status_code,
            _client_ip(request),
            request_id,
            elapsed_ms,
        )
        if elapsed_ms > self._SLOW_THRESHOLD_MS:
            logger.warning(log_msg + " [SLOW]", *log_args)
        else:
            logger.info(log_msg, *log_args)
        return response


# =====================================================================
# 全局异常处理器
# =====================================================================


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，统一 JSON 错误格式。

    * ``RequestValidationError`` → 422（Pydantic 校验失败，含字段级 detail）
    * ``Exception`` → 500（兜底，栈仅记日志不回客户端，防信息泄露）
    """

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        logger.info(
            "validation_error path=%s rid=%s errors=%s",
            request.url.path,
            rid,
            exc.errors(),
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "请求参数校验失败",
                "detail": _safe_validation_errors(exc.errors()),
                "request_id": rid,
            },
        )

    # 统一错误码体系（deep-spec 21）：DeadmanError / DeadmanHTTPException
    @app.exception_handler(DeadmanError)
    async def _deadman_error_handler(request: Request, exc: DeadmanError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        logger.warning(
            "deadman_error code=%s path=%s rid=%s: %s",
            exc.code,
            request.url.path,
            rid,
            exc.message,
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict(rid))

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        logger.exception(
            "unhandled_exception path=%s rid=%s: %s",
            request.url.path,
            rid,
            exc,
        )
        # P1-2: 上报未处理异常到 Sentry（含 request_id / path 关联标签）
        # 未初始化时 capture_exception 为 no-op，零开销
        try:
            from ..observability.sentry_init import capture_exception

            capture_exception(
                exc,
                request_id=rid,
                path=request.url.path,
                method=request.method,
            )
        except Exception:
            pass  # Sentry 上报失败绝不影响错误响应
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "服务器内部错误，请稍后重试",
                "request_id": rid,
            },
        )


def _safe_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清洗 Pydantic 校验错误，确保可 JSON 序列化。"""
    safe: list[dict[str, Any]] = []
    for err in errors:
        safe.append(
            {
                "loc": list(err.get("loc", [])),
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return safe


# =====================================================================
# 多租户中间件（P7.3 To B：按 JWT 绑定 tenant_id）
# =====================================================================


def _extract_bearer(authorization: str | None) -> str | None:
    """从 Authorization 头解析 Bearer token（大小写不敏感）。"""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


class TenantMiddleware(BaseHTTPMiddleware):
    """按 JWT 绑定租户上下文（multi 模式强制，single 模式恒走默认租户）。

    设计：
        - single（默认）：tenant_id 恒为 DEFAULT_TENANT_ID，进入 TenantContext，
          路径解析与现状完全一致（~/.deadman/），C 端零迁移。
        - multi：解析 Authorization Bearer token → tenant_id / org_role，
          未携带有效 token 或未绑机构时回退 DEFAULT_TENANT_ID（登录等公共路径）。
        - TenantContext 用 ContextVar 包裹 call_next，保证整个请求处理链路
          （含业务 store 的 resolve_tenant_path）路由到正确租户目录。
        - 失败安全：JWT 解析异常不阻塞请求，降级为默认租户。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        from ..infrastructure.multi_tenant import (
            DEFAULT_TENANT_ID,
            TenantContext,
            TenantInfo,
            get_tenant_registry,
            is_multi_tenant_enabled,
        )

        tenant_id = DEFAULT_TENANT_ID
        org_role: str | None = None

        if is_multi_tenant_enabled():
            token = _extract_bearer(request.headers.get("authorization"))
            if token:
                try:
                    # 复用 web.deps.get_jwt_manager：与 /api/auth/* 及
                    # require_org_role 使用同一 secret，token 才能互认
                    from ..web.deps import get_jwt_manager

                    payload = get_jwt_manager().verify(token)
                except Exception:  # 失败安全：解析异常降级默认租户
                    payload = None
                if payload:
                    tenant_id = payload.get("tenant_id") or DEFAULT_TENANT_ID
                    org_role = payload.get("org_role")

        registry = get_tenant_registry()
        tenant = registry.get(tenant_id) or TenantInfo(tenant_id=tenant_id, name="default")

        with TenantContext(tenant):
            request.state.tenant_id = tenant_id
            request.state.org_role = org_role
            return await call_next(request)


# =====================================================================
# Prometheus HTTP 指标中间件（RED: Rate / Errors / Duration）
# =====================================================================


# 请求总数 Counter（按 method/path_template/status 维度）
http_requests_total = Counter(
    "http_requests_total",
    "HTTP 请求总数（按方法/路由模板/状态码）",
    ["method", "path", "status"],
)

# 请求延迟 Histogram（标准 SLO 桶：50ms~10s）
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求处理耗时（秒）",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def _route_template(request: Request) -> str:
    """提取路由模板（如 /api/ending-note/{section}），避免高基数路径标签。

    用路由模板而非实际路径，防止 /api/cases/<uuid> 之类的路径
    产生无限多的 label 组合（Prometheus 高基数爆炸）。
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path_format"):
        try:
            return route.path_format
        except Exception:  # pragma: no cover
            pass
    return request.url.path


class PrometheusMetricsMiddleware(BaseHTTPMiddleware):
    """采集 HTTP RED 指标（Rate/Errors/Duration），供 /metrics 端点导出。

    使用 prometheus_client 官方库（Counter + Histogram），自动生成
    标准 Prometheus exposition 格式，兼容 Grafana / Prometheus 抓取。

    指标：
    * http_requests_total{method, path, status} —— 请求计数
    * http_request_duration_seconds{method, path} —— 延迟直方图
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # 健康探针/文档路径不计入指标，避免噪音
        path = request.url.path
        if path.startswith(_RATE_EXEMPT_PREFIXES):
            return await call_next(request)

        method = request.method
        route_tpl = _route_template(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            http_requests_total.labels(method=method, path=route_tpl, status=status).inc()
            http_request_duration_seconds.labels(method=method, path=route_tpl).observe(
                time.perf_counter() - start
            )
            raise

        elapsed = time.perf_counter() - start
        http_requests_total.labels(method=method, path=route_tpl, status=status).inc()
        http_request_duration_seconds.labels(method=method, path=route_tpl).observe(elapsed)
        return response


def export_http_metrics() -> tuple[str, str]:
    """导出 Prometheus HTTP 指标。

    Returns:
        (body, content_type) —— body 为 Prometheus 文本格式（str），content_type 为标准头
    """
    raw = generate_latest()
    body = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return body, CONTENT_TYPE_LATEST


# =====================================================================
# 中间件注册入口
# =====================================================================


def register_middlewares(app: FastAPI) -> None:
    """注册全部 FastAPI 中间件（按从外到内顺序）。

    顺序说明（Starlette 中间件**后添加的先执行**响应阶段，但请求阶段**先添加的先执行**）：
    * GZip —— 最外层，压缩响应
    * SecurityHeaders —— 注入安全头
    * PrometheusMetrics —— 采集 HTTP RED 指标（需在日志前，确保所有请求被采）
    * RequestLogging —— 记录访问日志 + 注入 request_id（需在限流前，确保被限流请求也有日志）
    * RateLimit —— 限流
    * Tenant —— 多租户绑定（最内层，紧贴路由，确保业务 store 全程在租户上下文中）

    实际请求流向：GZip → SecurityHeaders → PrometheusMetrics → RequestLogging → RateLimit → Tenant → 路由
    """
    # GZip 压缩（>1KB 的响应才压缩，避免小响应 CPU 浪费）
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # 安全响应头
    app.add_middleware(SecurityHeadersMiddleware)
    # Prometheus HTTP RED 指标
    app.add_middleware(PrometheusMetricsMiddleware)
    # 结构化访问日志 + 请求 ID
    app.add_middleware(RequestLoggingMiddleware)
    # 限流（复用 web/rate_limiter.RateLimiter）
    app.add_middleware(RateLimitMiddleware)
    # 多租户绑定（最内层：紧贴路由，业务 store 全程在租户上下文中）
    app.add_middleware(TenantMiddleware)
