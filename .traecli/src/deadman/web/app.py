"""FastAPI 主应用 —— deadman AG-UI Web Server。

迁移自 ``web/server.py``（stdlib http.server），获得：

* 自动 OpenAPI 文档（``/docs`` 与 ``/redoc``）
* Pydantic 请求体校验
* 原生 async + 依赖注入
* ``StreamingResponse`` 实现 SSE

设计原则：
* **不修改** ``web/server.py``（旧实现保留为 fallback，旧测试继续跑）。
* **复用** 现有业务模块（``auth`` / ``vault`` / ``ending_note`` /
  ``deadman_switch`` …）与 ``web/server.py`` 的 ``web_server`` 单例
  （``_handle_chat`` / ``_stream_chat`` / ``_conversation_stats`` 等复杂逻辑）。
* **保持 API 路径不变**，前端无需改造。

启动::

    uvicorn deadman.web.app:app --host 0.0.0.0 --port 8002
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from ..config import settings
from .deps import get_current_user, get_jwt_manager, get_optional_user, get_user_store

logger = logging.getLogger(__name__)

# 静态文件目录（与 web/server.py 同源）
_STATIC_DIR = Path(__file__).parent / "static"

# 复用现有 WebServer 单例 —— 复用 _handle_chat / _stream_chat /
# _record_conversation_stats / _conversation_stats 等复杂业务逻辑，
# 避免重复造轮子。
from .server import web_server  # noqa: E402

# 移动端 UA 关键字（与 web/server.py 一致）
_MOBILE_UA = ("android", "iphone", "ipod", "windows phone", "mobile")


# =====================================================================
# Pydantic 请求模型（与 web/schemas.py 对齐，保持 API 行为一致）
# =====================================================================


class RegisterRequest(BaseModel):
    email: str = Field(..., description="注册邮箱")
    password: str = Field(..., description="登录密码")
    display_name: str | None = Field(default=None, description="显示名称")

    model_config = {"extra": "ignore"}


class LoginRequest(BaseModel):
    email: str = Field(..., description="登录邮箱")
    password: str = Field(..., description="登录密码")

    model_config = {"extra": "ignore"}


class ChatRequest(BaseModel):
    query: str = Field(..., description="用户输入文本")
    agent: str | None = Field(default=None, description="目标智能体 ID")
    history: list[Any] | None = Field(default=None, description="对话历史")

    model_config = {"extra": "ignore"}


# =====================================================================
# 工具函数
# =====================================================================


def _token_expiry_iso(jwt_mgr, token: str) -> str:
    """从 token 解析 exp 并转为 ISO 时间戳（与 web/server.py 一致）"""
    payload = jwt_mgr.verify(token)
    if payload is None:
        return ""
    exp = payload.get("exp", 0)
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _disclaimer_footer() -> str:
    """所有 Phase 9 响应附带的 disclaimer 字段（transparency-framework）"""
    from ..disclaimer.text import DisclaimerBuilder

    return DisclaimerBuilder.for_web_footer()


def _ending_note_disclaimer() -> str:
    return (
        "终活笔记不是法律文件，不替代遗嘱/信托/医疗预嘱；"
        "如需法律效力，请咨询律师/公证处办理正式文件。"
    )


# =====================================================================
# Dead Man Switch auto-ticker（复用 web/server.py 的实现）
# =====================================================================


def _maybe_start_switch_auto_ticker() -> threading.Thread | None:
    """启动 SwitchAutoTicker 后台线程（复用 web/server.py 的实现）"""
    from .server import _maybe_start_switch_auto_ticker as _start

    return _start()


def _stop_switch_auto_ticker(thread: threading.Thread | None) -> None:
    from .server import _stop_switch_auto_ticker as _stop

    _stop(thread)


# =====================================================================
# SSE 流式：wfile 适配器（让现有 _stream_chat 写入 async 生成器）
# =====================================================================


class _WfileAdapter:
    """把 ``wfile.write(bytes)`` / ``wfile.flush()`` 桥接到 asyncio.Queue。

    现有 ``web_server._stream_chat(wfile, ...)`` 期望 ``wfile`` 提供
    同步 ``write(bytes)`` 与 ``flush()`` 接口。本适配器把每次写入
    投递到队列，由 :func:`StreamingResponse` 异步消费。
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def write(self, data: Any) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif not isinstance(data, (bytes, bytearray)):
            data = str(data).encode("utf-8")
        self.queue.put_nowait(bytes(data))
        return len(data)

    def flush(self) -> None:  # noqa: D401 - 兼容接口
        pass


# =====================================================================
# lifespan：启动 / 停止 SwitchAutoTicker
# =====================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan：启动 Dead Man Switch 自动 tick 后台调度器"""
    auto_tick_thread = _maybe_start_switch_auto_ticker()
    try:
        yield
    finally:
        _stop_switch_auto_ticker(auto_tick_thread)


# =====================================================================
# FastAPI 应用
# =====================================================================


def _build_app() -> FastAPI:
    cors_origins_raw = os.getenv("DEADMAN_CORS_ORIGINS", "*").strip()
    if not cors_origins_raw:
        cors_origins_raw = "*"
    if cors_origins_raw == "*":
        allow_origins = ["*"]
    else:
        allow_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

    app = FastAPI(
        title="deadman",
        version="5.1.0",
        description="身后事多智能体引导平台 — AG-UI Web API",
        lifespan=lifespan,
    )
    # CORS（最外层，确保预检请求不被限流拦截）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    )
    # 企业级横切：GZip / 安全头 / 访问日志 / 限流（复用已有组件）
    from .middleware import register_exception_handlers, register_middlewares

    register_middlewares(app)
    register_exception_handlers(app)
    return app


app = _build_app()


# =====================================================================
# Kubernetes 风格健康探针：liveness(/healthz) + readiness(/readyz)
# =====================================================================


@app.get("/healthz", tags=["ops"], include_in_schema=False)
async def healthz():
    """存活探针（liveness）—— 进程存活即 200，不检查依赖。

    Kubernetes 用此判断是否需要重启容器；失败才重启，故不应因依赖抖动误杀。
    """
    return {"status": "alive", "service": "deadman", "version": "5.1.0"}


@app.get("/readyz", tags=["ops"], include_in_schema=False)
async def readyz():
    """就绪探针（readiness）—— 检查关键依赖是否就绪，决定是否接流量。

    检查项：
    * 数据目录可写（auth/vault/ending_note/deadman_switch 共用 ~/.deadman）
    * FastAPI app 已初始化（路由非空）

    任一失败返回 503，Kubernetes 将停止把流量路由到本实例。
    """
    checks: dict[str, str] = {}
    ok = True

    # 数据目录可写检查
    try:
        data_root = Path.home() / ".deadman"
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".readyz_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks["data_dir"] = "ok"
    except Exception as exc:
        checks["data_dir"] = f"fail: {exc}"
        ok = False

    # 路由已加载检查
    try:
        route_count = len(app.routes)
        checks["routes"] = "ok" if route_count > 0 else "fail: no routes"
        if route_count == 0:
            ok = False
    except Exception as exc:
        checks["routes"] = f"fail: {exc}"
        ok = False

    status_code = 200 if ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if ok else "not_ready",
            "checks": checks,
            "version": "5.1.0",
        },
    )


# =====================================================================
# 静态文件 / 文档页面
# =====================================================================


@app.get("/", include_in_schema=False)
async def root_index(request: Request):
    """GET / → index.html（移动端 UA 跳转 /m）"""
    ua = request.headers.get("user-agent", "")
    ua_lower = ua.lower()
    if any(k in ua_lower for k in _MOBILE_UA) and "ipad" not in ua_lower:
        return RedirectResponse(url="/m", status_code=302)
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


@app.get("/m", include_in_schema=False)
@app.get("/m/", include_in_schema=False)
@app.get("/mobile.html", include_in_schema=False)
async def mobile_index():
    return FileResponse(_STATIC_DIR / "mobile.html", media_type="text/html; charset=utf-8")


@app.get("/manifest.json", include_in_schema=False)
async def manifest_json():
    return FileResponse(
        _STATIC_DIR / "manifest.json",
        media_type="application/manifest+json; charset=utf-8",
    )


@app.get("/sw.js", include_in_schema=False)
async def sw_js():
    return FileResponse(
        _STATIC_DIR / "sw.js", media_type="application/javascript; charset=utf-8"
    )


@app.get("/mobile.js", include_in_schema=False)
async def mobile_js():
    return FileResponse(
        _STATIC_DIR / "mobile.js",
        media_type="application/javascript; charset=utf-8",
    )


_DOCS_DISCLAIMER = (
    "本页面内容由 deadman 平台整理，不替代法律/医疗/财务专业意见。"
    "具体条款以最新版本为准。"
)


def _render_docs_page(name: str) -> HTMLResponse:
    """渲染 docs/<name>.md 为简单 HTML（与 web/server.py 一致，无 markdown 依赖）"""
    docs_dir = settings.project_root.parent / "docs"
    md_path = docs_dir / f"{name}.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到文档: {name}")
    try:
        raw = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc
    escaped = (
        raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    html = (
        "<!DOCTYPE html><html lang='zh-CN'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>{name} - deadman</title>"
        "<style>"
        "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        "background:#faf9f7;color:#1a1a1a;margin:0;padding:24px;line-height:1.7}"
        ".doc-wrap{max-width:820px;margin:0 auto;background:#fff;"
        "padding:32px 40px;border:1px solid #e4e0d8;border-radius:4px}"
        ".doc-back{display:inline-block;margin-bottom:16px;color:#6b5d4f;"
        "text-decoration:none;font-size:13px}"
        ".doc-back:hover{color:#4a3f35}"
        "pre{white-space:pre-wrap;word-wrap:break-word;font-family:inherit;"
        "font-size:14px;line-height:1.7;margin:0}"
        ".doc-footer{margin-top:24px;padding-top:16px;border-top:1px solid #e4e0d8;"
        "font-size:11px;color:#8a8a8a;text-align:center}"
        "@media (max-width:768px){.doc-wrap{padding:20px}}"
        "</style></head><body>"
        "<div class='doc-wrap'>"
        f"<a class='doc-back' href='/'>← 返回 deadman</a>"
        f"<pre>{escaped}</pre>"
        f"<div class='doc-footer'>{_DOCS_DISCLAIMER}</div>"
        "</div></body></html>"
    )
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


@app.get("/privacy", include_in_schema=False)
async def docs_privacy():
    return _render_docs_page("privacy")


@app.get("/terms", include_in_schema=False)
async def docs_terms():
    return _render_docs_page("terms")


@app.get("/support", include_in_schema=False)
async def docs_support():
    return _render_docs_page("support")


# =====================================================================
# 健康检查 / 信息路由
# =====================================================================


@app.get("/api/health", tags=["info"])
async def api_health():
    return {"status": "ok", "service": "ag-ui"}


@app.get("/api/whoami", tags=["info"])
async def api_whoami():
    return web_server._handle_whoami()


@app.get("/api/agents", tags=["info"])
async def api_agents():
    """返回智能体列表（6 个，与 web/server.py 一致）"""
    agents = [
        {"id": "death-aftercare", "name": "身后事流程引导员"},
        {"id": "legal-advisor", "name": "法律咨询智能体"},
        {"id": "financial-analyst", "name": "财务分析智能体"},
        {"id": "policy-researcher", "name": "政策研究智能体"},
        {"id": "cross-border-specialist", "name": "跨境事务智能体"},
        {"id": "medical-guide", "name": "医疗导航智能体"},
    ]
    return {"agents": agents}


@app.get("/api/tools", tags=["info"])
async def api_tools():
    """返回 MCP 工具列表"""
    try:
        from ..mcp_server.server import mcp

        return {"tools": mcp.list_tools()}
    except Exception as exc:
        return {"tools": [], "error": str(exc)}


@app.get("/api/disclaimer", tags=["info"])
async def api_disclaimer(
    scenario: str | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
):
    """GET /api/disclaimer - 返回免责告知

    无参数：完整开场告知；``?scenario=`` 场景化简短提醒；``?format=footer`` Web 页脚
    """
    from ..disclaimer.text import DisclaimerBuilder

    try:
        if format == "footer":
            text = DisclaimerBuilder.for_web_footer()
            kind = "footer"
        elif scenario:
            text = DisclaimerBuilder.short_reminder(scenario)
            kind = f"scenario:{scenario}"
        else:
            text = DisclaimerBuilder.full_opening()
            kind = "full_opening"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"text": text, "kind": kind, "disclaimer": _disclaimer_footer()}


@app.get("/api/hotlines", tags=["info"])
async def api_hotlines(
    province: str | None = Query(default=None),
    region: str | None = Query(default=None, description="province 别名"),
    function: str | None = Query(default=None),
):
    """GET /api/hotlines - 热线查询（``province`` / ``region`` 任选其一）"""
    from ..hotlines.lookup import HotlineLookup

    province_val = province or region
    lookup = HotlineLookup()
    results = lookup.lookup(province_val, function)
    return {
        "hotlines": results,
        "count": len(results),
        "query": {"province": province_val, "function": function},
        "disclaimer": _disclaimer_footer(),
    }


@app.get("/api/institutions", tags=["info"])
async def api_institutions(
    province: str | None = Query(default=None),
    region: str | None = Query(default=None, description="province 别名"),
    city: str | None = Query(default=None),
    type: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
):
    """GET /api/institutions - 机构查询"""
    from ..institutions.store import InstitutionStore

    province_val = province or region
    store = InstitutionStore()
    results = store.search(province_val, city, type, keyword)
    return {
        "institutions": [i.to_dict() for i in results],
        "count": len(results),
        "query": {
            "province": province_val,
            "city": city,
            "type": type,
            "keyword": keyword,
        },
        "disclaimer": _disclaimer_footer(),
    }


@app.get("/api/institutions/{institution_id}", tags=["info"])
async def api_institution_by_id(institution_id: str):
    """GET /api/institutions/<id> - 机构详情"""
    from ..institutions.store import InstitutionStore

    store = InstitutionStore()
    inst = store.get(institution_id)
    if inst is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "机构不存在",
                "institution_id": institution_id,
                "disclaimer": _disclaimer_footer(),
            },
        )
    payload = inst.to_dict()
    payload["needs_verification_warning"] = inst.needs_verification_warning()
    payload["disclaimer"] = _disclaimer_footer()
    return payload


# =====================================================================
# 认证路由
# =====================================================================


@app.post("/api/auth/register", tags=["auth"])
async def auth_register(req: RegisterRequest):
    """POST /api/auth/register → {user_id, token, expires_at}"""
    store = get_user_store()
    store.password_min_length = settings.password_min_length
    try:
        user = store.register(req.email, req.password, req.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    jwt_mgr = get_jwt_manager()
    token = jwt_mgr.issue(user)
    expires_at = _token_expiry_iso(jwt_mgr, token)
    return {"user_id": user["user_id"], "token": token, "expires_at": expires_at}


@app.post("/api/auth/login", tags=["auth"])
async def auth_login(req: LoginRequest):
    """POST /api/auth/login → {user_id, token, expires_at, display_name} 或 401"""
    store = get_user_store()
    user = store.verify(req.email, req.password)
    if user is None:
        # 防枚举：不区分"邮箱不存在" vs "密码错"
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    jwt_mgr = get_jwt_manager()
    token = jwt_mgr.issue(user)
    expires_at = _token_expiry_iso(jwt_mgr, token)
    return {
        "user_id": user["user_id"],
        "token": token,
        "expires_at": expires_at,
        "display_name": user.get("display_name", ""),
    }


@app.get("/api/auth/me", tags=["auth"])
async def auth_me(user: dict = Depends(get_current_user)):
    """GET /api/auth/me → {user_id, email, display_name, role, ...}"""
    return user


@app.post("/api/auth/refresh", tags=["auth"])
async def auth_refresh(authorization: str | None = Header(default=None)):
    """POST /api/auth/refresh → {token, expires_at} 或 401

    手动解析 Authorization 头（不强制 user 存在，仅刷新 token）。
    """
    jwt_mgr = get_jwt_manager()
    new_token = jwt_mgr.refresh(_extract_bearer(authorization) or "")
    if new_token is None:
        raise HTTPException(status_code=401, detail="token 无效或无需刷新")
    expires_at = _token_expiry_iso(jwt_mgr, new_token)
    return {"token": new_token, "expires_at": expires_at}


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


# =====================================================================
# 对话路由
# =====================================================================


@app.post("/api/chat", tags=["chat"])
async def api_chat(req: ChatRequest, user: dict | None = Depends(get_optional_user)):
    """POST /api/chat → {response, agent, metadata}

    复用 ``web_server._handle_chat`` —— 走完整 orchestration graph 规则链。
    优先用认证用户（token 有效），否则降级 anonymous。
    """
    query_text = req.query
    agent = req.agent or "death-aftercare"
    history = req.history or []
    user_id = user.get("user_id") if user else None
    if not query_text:
        return {"error": "query 不能为空"}
    return await web_server._handle_chat(agent, query_text, history, user_id)


@app.post("/api/whoami_post", tags=["chat"], include_in_schema=False)
async def api_whoami_post():
    return web_server._handle_whoami()


@app.get("/api/stream", tags=["chat"])
async def api_stream(
    query: str = Query(default=""),
    agent: str = Query(default="death-aftercare"),
    user: dict | None = Depends(get_optional_user),
):
    """GET /api/stream?query=...&agent=... → SSE 流式响应

    复用 ``web_server._stream_chat`` —— 通过 :class:`_WfileAdapter`
    把同步 ``wfile.write`` 桥接到 ``StreamingResponse`` 的 async 生成器。
    """
    user_id = user.get("user_id") if user else None

    async def event_stream():
        adapter = _WfileAdapter()
        # 在后台 task 跑 _stream_chat，主协程从队列消费
        task = asyncio.create_task(
            web_server._stream_chat(adapter, query, agent, user_id)
        )
        try:
            while True:
                chunk = await adapter.queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =====================================================================
# 运维 / 可观测路由
# =====================================================================


@app.get("/metrics", tags=["ops"], include_in_schema=False)
async def metrics():
    """Prometheus 指标端点"""
    try:
        from ..observability.metrics import metrics_collector

        text = metrics_collector.export_prometheus()
        return PlainTextResponse(
            text, media_type="text/plain; version=0.0.4; charset=utf-8"
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/obs/dashboard", tags=["ops"])
async def obs_dashboard():
    """可观测性看板（结构化 JSON）"""
    try:
        from ..observability import metrics_collector

        return metrics_collector.get_dashboard()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/slo", tags=["ops"])
async def slo_dashboard():
    """SLI/SLO 看板"""
    try:
        from ..observability import metrics as m_module
        from ..observability.metrics import SLO_TARGETS, metrics_collector

        if not m_module.SLO_DASHBOARD_ENABLED:
            return {
                "enabled": False,
                "sli": {},
                "slo": {},
                "targets": {},
                "message": "SLO dashboard disabled (DEADMAN_SLO_DASHBOARD_ENABLED=0)",
            }
        return {
            "enabled": True,
            "sli": metrics_collector.compute_sli(),
            "slo": metrics_collector.compute_slo_status(),
            "targets": SLO_TARGETS,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/dashboard", tags=["ops"])
async def dashboard():
    """P9：对话维度 dashboard 概览页（agent/risk/span/token/termination）"""
    try:
        return copy.deepcopy(web_server._conversation_stats)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/llm/health", tags=["ops"], include_in_schema=False)
async def llm_health():
    """读取 data/llm_health.json"""
    data_file = settings.project_root / "data" / "llm_health.json"
    if data_file.exists():
        try:
            return json.loads(data_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc
    return {
        "status": "no_data",
        "message": "llm_health.json 尚未生成，请先运行对应 CLI 命令",
    }


@app.get("/api/memory/state", tags=["ops"])
async def memory_state():
    """记忆 4 层状态"""
    try:
        from ..memory.manager import MemoryManager

        mgr = MemoryManager()
        return {
            "working": len(mgr.working._turns) if hasattr(mgr.working, "_turns") else 0,
            "episodic": len(mgr.episodic._store),
            "semantic": len(mgr.semantic.facts),
            "semantic_profiles": len(mgr.semantic.user_profiles),
            "semantic_contradictions": len(mgr.semantic.pending_contradictions),
            "procedural": len(mgr.procedural._procedures)
            if hasattr(mgr.procedural, "_procedures")
            else 0,
            "graphiti_enabled": mgr.graphiti is not None,
            "lightrag_enabled": mgr.lightrag is not None,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/deploy/check", tags=["ops"])
async def deploy_check():
    """部署工件校验"""
    import yaml

    project_root = settings.project_root.parent
    docker_dir = settings.project_root / "docker"
    artifacts = [
        ("Dockerfile", project_root / "Dockerfile"),
        ("docker-compose.yml", project_root / "docker-compose.yml"),
        ("entrypoint.sh", docker_dir / "entrypoint.sh"),
        ("healthcheck.py", docker_dir / "healthcheck.py"),
    ]
    results = [
        {"name": name, "exists": path.exists(), "path": str(path)}
        for name, path in artifacts
    ]
    compose_path = project_root / "docker-compose.yml"
    compose_ok = False
    services: list[str] = []
    if compose_path.exists():
        try:
            with open(compose_path, encoding="utf-8") as f:
                compose = yaml.safe_load(f) or {}
            services = list((compose.get("services") or {}).keys())
            compose_ok = True
        except Exception as exc:
            logger.debug("docker-compose.yml 解析失败: %s", exc)
    return {
        "artifacts": results,
        "compose_valid": compose_ok,
        "compose_services": services,
    }


@app.get("/api/health/all", tags=["ops"])
async def health_all():
    """全领域健康汇总（读取所有 data/*_health.json）"""
    data_dir = settings.project_root / "data"
    domains = [
        "llm", "prompt", "rule", "agent", "knowledge",
        "eval", "tool", "mcp", "obs", "memory",
        "a2a", "deploy", "reflexion", "skill",
    ]
    summary: dict[str, Any] = {}
    for domain in domains:
        hf = data_dir / f"{domain}_health.json"
        if hf.exists():
            try:
                summary[domain] = json.loads(hf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("健康文件解析失败 domain=%s: %s", domain, exc)
                summary[domain] = {"status": "parse_error"}
        else:
            summary[domain] = {"status": "no_data"}
    return summary


# =====================================================================
# 终活笔记路由（需要认证）
# =====================================================================


@app.get("/api/ending-note", tags=["ending-note"])
async def ending_note_get(user: dict = Depends(get_current_user)):
    """GET /api/ending-note - 获取我的笔记"""
    from ..ending_note.store import EndingNoteStore

    user_id = user["user_id"]
    store = EndingNoteStore()
    note = store.load(user_id)
    if note is None:
        return JSONResponse(
            status_code=404,
            content={
                "note": None,
                "message": "尚无终活笔记，请调 POST /api/ending-note/section 开始填写",
                "disclaimer": _ending_note_disclaimer(),
            },
        )
    return {"note": note.to_dict(), "disclaimer": _ending_note_disclaimer()}


@app.get("/api/ending-note/guide/next", tags=["ending-note"])
async def ending_note_guide_next(
    user: dict = Depends(get_current_user),
    chapter: str | None = Query(default=None, description="章节（保留参数，与前端兼容）"),
):
    """GET /api/ending-note/guide/next - 获取下一章引导问题"""
    from ..ending_note.guide import EndingNoteGuide
    from ..ending_note.models import EndingNote as EN
    from ..ending_note.store import EndingNoteStore

    user_id = user["user_id"]
    store = EndingNoteStore()
    note = store.load(user_id) or EN.new(user_id)
    guide = EndingNoteGuide(store=store)
    section, title, question = guide.next_question(note)
    return {
        "section": section,
        "title": title,
        "question": question,
        "disclaimer": _ending_note_disclaimer(),
    }


@app.get("/api/ending-note/completion", tags=["ending-note"])
async def ending_note_completion(user: dict = Depends(get_current_user)):
    """GET /api/ending-note/completion - 填写完整度"""
    from ..ending_note.guide import EndingNoteGuide
    from ..ending_note.models import EndingNote as EN
    from ..ending_note.store import EndingNoteStore

    user_id = user["user_id"]
    store = EndingNoteStore()
    note = store.load(user_id) or EN.new(user_id)
    guide = EndingNoteGuide(store=store)
    rate = guide.completion_rate(note)
    return {"completion": rate, "disclaimer": _ending_note_disclaimer()}


@app.get("/api/ending-note/shared-with-me", tags=["ending-note"])
async def ending_note_shared_with_me(user: dict = Depends(get_current_user)):
    """GET /api/ending-note/shared-with-me - 共享给我的笔记"""
    from ..ending_note.store import EndingNoteStore

    store = EndingNoteStore()
    notes = store.list_shared_with_me(user["user_id"])
    return {
        "notes": [n.to_dict() for n in notes],
        "count": len(notes),
        "disclaimer": _ending_note_disclaimer(),
    }


class EndingNoteSectionRequest(BaseModel):
    section: str
    answer: dict[str, Any]


@app.post("/api/ending-note/section", tags=["ending-note"])
async def ending_note_section(
    req: EndingNoteSectionRequest, user: dict = Depends(get_current_user)
):
    """POST /api/ending-note/section - 保存章节"""
    from ..ending_note.guide import EndingNoteGuide
    from ..ending_note.models import EndingNote as EN
    from ..ending_note.store import EndingNoteStore

    user_id = user["user_id"]
    store = EndingNoteStore()
    guide = EndingNoteGuide(store=store)
    note = store.load(user_id) or EN.new(user_id)
    try:
        note = guide.save_answer(note, req.section, req.answer)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "disclaimer": _ending_note_disclaimer()},
        ) from exc
    store.save(note)

    payload: dict[str, Any] = {
        "ok": True,
        "note": note.to_dict(),
        "disclaimer": _ending_note_disclaimer(),
    }
    safety = note.safety_flags or {}
    if safety.get("contains_suicidal_ideation"):
        payload["safety_alert"] = {
            "severity": "high",
            "message": (
                "我注意到你刚才说的话让我有些担心你的安全。"
                "在你继续填写笔记之前，"
                "请考虑联系当地心理危机干预热线或急救电话。"
                "我没办法替你保密这件事——你的安全比这份笔记更重要。"
            ),
            "stop_flow": True,
        }
    return payload


class EndingNoteShareRequest(BaseModel):
    target_user_id: str
    sections: list[str] | None = None


@app.post("/api/ending-note/share", tags=["ending-note"])
async def ending_note_share(
    req: EndingNoteShareRequest, user: dict = Depends(get_current_user)
):
    """POST /api/ending-note/share - 共享给家庭成员"""
    from ..ending_note.store import EndingNoteStore

    store = EndingNoteStore()
    try:
        store.share_with(user["user_id"], req.target_user_id, req.sections)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "shared_with": req.target_user_id,
        "sections": req.sections,
        "disclaimer": _ending_note_disclaimer(),
    }


@app.delete("/api/ending-note/share", tags=["ending-note"])
async def ending_note_unshare(
    user: dict = Depends(get_current_user),
    target_user_id: str | None = Query(default=None, alias="target_user_id"),
    to_user: str | None = Query(default=None, description="target_user_id 别名"),
):
    """DELETE /api/ending-note/share?target_user_id=xxx - 取消共享"""
    from ..ending_note.store import EndingNoteStore

    target = target_user_id or to_user
    if not target:
        raise HTTPException(
            status_code=400,
            detail={"error": "缺少 target_user_id", "disclaimer": _ending_note_disclaimer()},
        )
    store = EndingNoteStore()
    store.unshare(user["user_id"], target)
    return {
        "ok": True,
        "unshared_with": target,
        "disclaimer": _ending_note_disclaimer(),
    }


class EndingNoteSectionDeleteRequest(BaseModel):
    section_id: str


@app.delete("/api/ending-note/section", tags=["ending-note"])
async def ending_note_section_delete(
    user: dict = Depends(get_current_user),
    chapter: str | None = Query(default=None, description="section_id 别名"),
    payload: EndingNoteSectionDeleteRequest | None = None,
):
    """DELETE /api/ending-note/section - 删除（清空）某个章节

    支持两种入参：query ``?chapter=`` 或 body ``{section_id: ...}``，
    与旧实现兼容（旧实现读 body.section_id）。
    """
    from ..ending_note.store import EndingNoteStore

    section_key = chapter
    if payload is not None and payload.section_id:
        section_key = payload.section_id
    if not section_key:
        raise HTTPException(
            status_code=400,
            detail={"error": "缺少 section_id", "disclaimer": _ending_note_disclaimer()},
        )
    store = EndingNoteStore()
    try:
        ok = store.delete_section(user["user_id"], section_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "disclaimer": _ending_note_disclaimer()},
        ) from exc
    if not ok:
        return JSONResponse(
            status_code=404,
            content={"error": "笔记不存在", "disclaimer": _ending_note_disclaimer()},
        )
    return {
        "ok": True,
        "deleted_section": section_key,
        "disclaimer": _ending_note_disclaimer(),
    }


class EndingNoteTriggerRequest(BaseModel):
    trigger_type: str


@app.post("/api/ending-note/trigger", tags=["ending-note"])
async def ending_note_trigger(
    req: EndingNoteTriggerRequest, user: dict = Depends(get_current_user)
):
    """POST /api/ending-note/trigger - 触发投递"""
    from ..ending_note.store import EndingNoteStore

    store = EndingNoteStore()
    result = store.trigger_delivery(user["user_id"], req.trigger_type)
    payload = dict(result)
    payload["disclaimer"] = _ending_note_disclaimer()
    if req.trigger_type == "death_confirmation" and not result.get("delivered"):
        payload["safety_notice"] = (
            "死亡确认触发已记录。"
            "等待 7 天是为了避免在情绪冲动下做出不可逆的投递决定。"
            "等待期内你可以随时取消。"
        )
    return payload


# =====================================================================
# 保险库路由（需要认证）
# =====================================================================


@app.get("/api/vault/items", tags=["vault"])
async def vault_items_list(user: dict = Depends(get_current_user)):
    """GET /api/vault/items - 列出我的条目"""
    from ..vault.store import VaultStore

    store = VaultStore()
    items = store.list_items(user["user_id"], user["user_id"])
    return {"items": items}


@app.get("/api/vault/items/{item_id}", tags=["vault"])
async def vault_item_get(item_id: str, user: dict = Depends(get_current_user)):
    """GET /api/vault/items/<id> - 获取条目详情"""
    from ..vault.store import VaultStore

    store = VaultStore()
    item = store.get_item(item_id, user["user_id"])
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在或无权限")
    return item.to_index_dict()


class VaultItemAddRequest(BaseModel):
    type: str = "note"
    title: str = ""
    content: str | None = None
    beneficiary_user_ids: list[str] | None = None
    delivery_trigger: str = "manual"
    delivery_date: str | None = None
    metadata: dict[str, Any] | None = None


@app.post("/api/vault/items", tags=["vault"], status_code=201)
async def vault_item_add(req: VaultItemAddRequest, user: dict = Depends(get_current_user)):
    """POST /api/vault/items - 添加条目"""
    from ..vault.store import VaultStore

    try:
        store = VaultStore()
        content: Any = req.content or ""
        if isinstance(content, str) and content.startswith("base64:"):
            content = base64.b64decode(content[len("base64:"):])
        delivery_date = None
        if req.delivery_date:
            try:
                delivery_date = datetime.fromisoformat(req.delivery_date)
            except (TypeError, ValueError):
                delivery_date = None
        item = store.add_item(
            owner_user_id=user["user_id"],
            type=req.type,
            title=req.title,
            content=content,
            beneficiary_user_ids=req.beneficiary_user_ids or [],
            delivery_trigger=req.delivery_trigger,
            delivery_date=delivery_date,
            metadata=req.metadata or {},
        )
        return item.to_index_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("vault item add failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class VaultItemUpdateRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    metadata: dict[str, Any] | None = None
    beneficiary_user_ids: list[str] | None = None
    delivery_trigger: str | None = None
    delivery_date: str | None = None


@app.put("/api/vault/items/{item_id}", tags=["vault"])
async def vault_item_update(
    item_id: str, req: VaultItemUpdateRequest, user: dict = Depends(get_current_user)
):
    """PUT /api/vault/items/<id> - 更新条目（仅 owner）"""
    from ..vault.store import VaultStore

    try:
        store = VaultStore()
        updates: dict[str, Any] = {}
        raw = req.model_dump(exclude_none=True)
        for field in (
            "title", "content", "metadata",
            "beneficiary_user_ids", "delivery_trigger",
            "delivery_date",
        ):
            if field in raw:
                updates[field] = raw[field]
        if updates.get("delivery_date"):
            try:
                updates["delivery_date"] = datetime.fromisoformat(
                    str(updates["delivery_date"])
                )
            except (TypeError, ValueError):
                pass
        item = store.update_item(item_id, user["user_id"], updates)
        if item is None:
            raise HTTPException(status_code=404, detail="条目不存在或无权限")
        return item.to_index_dict()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("vault item update failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.delete("/api/vault/items/{item_id}", tags=["vault"])
async def vault_item_delete(item_id: str, user: dict = Depends(get_current_user)):
    """DELETE /api/vault/items/<id> - 删除条目（仅 owner）"""
    from ..vault.store import VaultStore

    store = VaultStore()
    ok = store.delete_item(item_id, user["user_id"])
    if ok:
        return {"deleted": True}
    raise HTTPException(status_code=404, detail="条目不存在或无权限")


class VaultTriggerRequest(BaseModel):
    trigger_type: str = "manual"


@app.post("/api/vault/items/{item_id}/trigger", tags=["vault"])
async def vault_item_trigger(
    item_id: str, req: VaultTriggerRequest, user: dict = Depends(get_current_user)
):
    """POST /api/vault/items/<id>/trigger - 触发投递"""
    from ..vault.store import VaultStore

    store = VaultStore()
    result = store.trigger_delivery(item_id, req.trigger_type, user["user_id"])
    if result.get("content") is not None:
        result["content_b64"] = base64.b64encode(result["content"]).decode("ascii")
        result["content"] = None
    return result


@app.get("/api/vault/beneficiaries", tags=["vault"])
async def vault_beneficiaries(user: dict = Depends(get_current_user)):
    """GET /api/vault/beneficiaries - 列出我指定的受益人"""
    from ..vault.store import VaultStore

    store = VaultStore()
    return {"beneficiaries": store.list_beneficiaries(user["user_id"])}


@app.get("/api/vault/inherited", tags=["vault"])
async def vault_inherited(user: dict = Depends(get_current_user)):
    """GET /api/vault/inherited - 列出我能继承的"""
    from ..vault.store import VaultStore

    store = VaultStore()
    return {"inherited": store.list_inherited(user["user_id"])}


# =====================================================================
# 文档提取路由（需要认证）
# =====================================================================


@app.get("/api/documents", tags=["documents"])
async def documents_list(user: dict = Depends(get_current_user)):
    """GET /api/documents - 列出我的文档"""
    from ..doc_extract.extractor import DocumentExtractor

    extractor = DocumentExtractor()
    docs = extractor.list_my_documents(user["user_id"])
    return {"documents": [d.to_dict() for d in docs]}


@app.get("/api/documents/{doc_id}", tags=["documents"])
async def document_get(doc_id: str, user: dict = Depends(get_current_user)):
    """GET /api/documents/<id> - 文档详情"""
    from ..doc_extract.extractor import DocumentExtractor

    extractor = DocumentExtractor()
    doc = extractor.get_document(doc_id, user["user_id"])
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在或无权限")
    return doc.to_dict()


@app.delete("/api/documents/{doc_id}", tags=["documents"])
async def document_delete(doc_id: str, user: dict = Depends(get_current_user)):
    """DELETE /api/documents/<id> - 删除文档"""
    from ..doc_extract.extractor import DocumentExtractor

    extractor = DocumentExtractor()
    ok = extractor.delete_document(doc_id, user["user_id"])
    if ok:
        return {"deleted": True}
    raise HTTPException(status_code=404, detail="文档不存在或无权限")


@app.post("/api/documents/extract", tags=["documents"], status_code=201)
async def document_extract(
    user: dict = Depends(get_current_user),
    file: UploadFile | None = File(default=None),
    doc_type: str | None = Form(default=None),
):
    """POST /api/documents/extract - 上传文档并提取

    支持 ``multipart/form-data``（field: file, doc_type?）。
    """
    from ..doc_extract.extractor import DocumentExtractor

    filename = ""
    content: bytes = b""
    doc_type_hint = doc_type
    if file is not None:
        filename = file.filename or ""
        content = await file.read()
    else:
        raise HTTPException(status_code=400, detail="缺少 filename 或 content")
    if not filename or not content:
        raise HTTPException(status_code=400, detail="缺少 filename 或 content")
    try:
        extractor = DocumentExtractor()
        doc = await extractor.extract(
            owner_user_id=user["user_id"],
            filename=filename,
            content=content,
            doc_type_hint=doc_type_hint,
        )
        return doc.to_dict()
    except Exception as exc:
        logger.exception("document extract failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


# =====================================================================
# 案例（Decedent ID）路由（需要认证）
# =====================================================================


@app.get("/api/cases", tags=["cases"])
async def cases_list(user: dict = Depends(get_current_user)):
    """GET /api/cases - 列出我的案例"""
    from ..decedent_id.registry import DecedentRegistry

    reg = DecedentRegistry()
    cases = reg.list_cases(user["user_id"])
    return {"cases": [c.to_dict() for c in cases]}


@app.get("/api/cases/{case_id}", tags=["cases"])
async def case_get(case_id: str, user: dict = Depends(get_current_user)):
    """GET /api/cases/<id> - 案例详情"""
    from ..decedent_id.registry import DecedentRegistry

    reg = DecedentRegistry()
    case = reg.get_case(case_id, user["user_id"])
    if case is None:
        raise HTTPException(status_code=404, detail="案例不存在或无权限")
    return case.to_dict()


class CaseCreateRequest(BaseModel):
    decedent_alias: str = ""
    relationship: str = "其他"


@app.post("/api/cases", tags=["cases"], status_code=201)
async def case_create(req: CaseCreateRequest, user: dict = Depends(get_current_user)):
    """POST /api/cases - 创建案例"""
    from ..decedent_id.registry import DecedentRegistry

    try:
        reg = DecedentRegistry()
        case = reg.create_case(
            owner_user_id=user["user_id"],
            decedent_alias=req.decedent_alias,
            relationship=req.relationship,
        )
        return case.to_dict()
    except Exception as exc:
        logger.exception("case create failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class CaseEventRequest(BaseModel):
    event: str = ""
    agent: str = "unknown"
    notes: str | None = None


@app.post("/api/cases/{case_id}/events", tags=["cases"])
async def case_event_add(
    case_id: str, req: CaseEventRequest, user: dict = Depends(get_current_user)
):
    """POST /api/cases/<id>/events - 添加事件"""
    from ..decedent_id.registry import DecedentRegistry

    reg = DecedentRegistry()
    case = reg.add_event(
        case_id=case_id,
        owner_user_id=user["user_id"],
        event=req.event,
        agent=req.agent,
        notes=req.notes or "",
    )
    if case is None:
        raise HTTPException(status_code=404, detail="案例不存在或无权限")
    return case.to_dict()


@app.post("/api/cases/{case_id}/archive", tags=["cases"])
async def case_archive(case_id: str, user: dict = Depends(get_current_user)):
    """POST /api/cases/<id>/archive - 归档案例"""
    from ..decedent_id.registry import DecedentRegistry

    reg = DecedentRegistry()
    ok = reg.archive_case(case_id, user["user_id"])
    if ok:
        return {"archived": True}
    raise HTTPException(status_code=404, detail="案例不存在或无权限")


@app.get("/api/cases/{case_id}/timeline", tags=["cases"])
async def case_timeline(case_id: str, user: dict = Depends(get_current_user)):
    """GET /api/cases/<id>/timeline - 时间线"""
    from ..decedent_id.registry import DecedentRegistry

    reg = DecedentRegistry()
    timeline = reg.get_timeline(case_id, user["user_id"])
    return {"timeline": timeline}


# =====================================================================
# Dead Man Switch 路由（需要认证）
# =====================================================================


@app.get("/api/switch/status", tags=["switch"])
async def switch_status(user: dict = Depends(get_current_user)):
    """GET /api/switch/status - 查看状态"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record = store.load(user["user_id"])
    if record is None:
        raise HTTPException(status_code=404, detail="switch not initialized")
    payload = record.to_dict()
    if record.state.value == "CONFIRMED":
        payload["cooldown_remaining_days"] = store.cooldown_remaining_days(user["user_id"])
        payload["cooldown_passed"] = store.is_cooldown_passed(user["user_id"])
    return payload


@app.get("/api/switch/actions", tags=["switch"])
async def switch_actions(user: dict = Depends(get_current_user)):
    """GET /api/switch/actions - 列出待执行动作"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record = store.load(user["user_id"])
    if record is None:
        raise HTTPException(status_code=404, detail="switch not initialized")
    return {
        "pending_actions": record.pending_actions,
        "executed_actions": record.executed_actions,
        "state": record.state.value,
    }


class SwitchInitRequest(BaseModel):
    frequency: int = 30
    missed: int = 3
    window: int = 7
    cooldown: int = 7
    emergency_contacts: list[str] | None = None
    lawyer_id: str | None = None
    heir_ids: list[str] | None = None
    email: str | None = None
    phone: str | None = None


@app.post("/api/switch/init", tags=["switch"], status_code=201)
async def switch_init(req: SwitchInitRequest, user: dict = Depends(get_current_user)):
    """POST /api/switch/init - 初始化配置"""
    from ..deadman_switch.models import SwitchConfig
    from ..deadman_switch.store import SwitchStore

    config = SwitchConfig(
        check_in_frequency_days=req.frequency,
        missed_threshold=req.missed,
        verification_window_days=req.window,
        cooldown_days=max(req.cooldown, 7),
        emergency_contacts=list(req.emergency_contacts or []),
        lawyer_user_id=req.lawyer_id,
        heir_user_ids=list(req.heir_ids or []),
    )
    if req.email:
        config.set_email(str(req.email))
    if req.phone:
        config.set_phone(str(req.phone))
    store = SwitchStore()
    record = store.init_switch(user["user_id"], config)
    return record.to_dict()


class SwitchCheckinRequest(BaseModel):
    method: str = "web"


@app.post("/api/switch/checkin", tags=["switch"])
async def switch_checkin(req: SwitchCheckinRequest, user: dict = Depends(get_current_user)):
    """POST /api/switch/checkin - 用户 check-in"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record = store.record_check_in(user["user_id"], method=req.method)
    if record is None:
        raise HTTPException(status_code=404, detail="switch not initialized")
    return record.to_dict()


@app.post("/api/switch/tick", tags=["switch"])
async def switch_tick(user: dict = Depends(get_current_user)):
    """POST /api/switch/tick - 手动触发状态机检查（Cron 调用）"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record = store.tick(user["user_id"])
    if record is None:
        raise HTTPException(status_code=404, detail="switch not initialized")
    return {"state": record.state.value, "record": record.to_dict()}


class SwitchVerifyContactRequest(BaseModel):
    contact_id: str
    confirm: bool = False


@app.post("/api/switch/verify-contact", tags=["switch"])
async def switch_verify_contact(
    req: SwitchVerifyContactRequest, user: dict = Depends(get_current_user)
):
    """POST /api/switch/verify-contact"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record, msg = store.verify_emergency_contact(
        user["user_id"], str(req.contact_id), req.confirm
    )
    if record is None:
        raise HTTPException(status_code=404, detail=msg)
    return {"message": msg, "record": record.to_dict()}


class SwitchVerifyHeirRequest(BaseModel):
    heir_id: str
    confirm: bool = False


@app.post("/api/switch/verify-heir", tags=["switch"])
async def switch_verify_heir(
    req: SwitchVerifyHeirRequest, user: dict = Depends(get_current_user)
):
    """POST /api/switch/verify-heir"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record, msg = store.verify_heir(user["user_id"], str(req.heir_id), req.confirm)
    if record is None:
        raise HTTPException(status_code=404, detail=msg)
    return {"message": msg, "record": record.to_dict()}


class SwitchCancelRequest(BaseModel):
    reason: str = "user_cancelled"


@app.post("/api/switch/cancel", tags=["switch"])
async def switch_cancel(req: SwitchCancelRequest, user: dict = Depends(get_current_user)):
    """POST /api/switch/cancel"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record = store.cancel(user["user_id"], reason=req.reason)
    if record is None:
        raise HTTPException(status_code=404, detail="switch not initialized")
    return record.to_dict()


@app.post("/api/switch/execute", tags=["switch"])
async def switch_execute(user: dict = Depends(get_current_user)):
    """POST /api/switch/execute - 执行 CONFIRMED → EXECUTED（须过冷静期）"""
    from ..deadman_switch.actions import SwitchActionExecutor
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    executor = SwitchActionExecutor(store=store)
    try:
        return executor.execute_confirmed(user["user_id"])
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/switch/engage-lawyer", tags=["switch"])
async def switch_engage_lawyer(user: dict = Depends(get_current_user)):
    """POST /api/switch/engage-lawyer - 律师介入标记"""
    from ..deadman_switch.store import SwitchStore

    store = SwitchStore()
    record, msg = store.engage_lawyer(user["user_id"])
    if record is None:
        raise HTTPException(status_code=404, detail=msg)
    if msg != "lawyer_engaged":
        return JSONResponse(
            status_code=409,
            content={"success": False, "message": msg, "record": record.to_dict()},
        )
    return {"success": True, "message": msg, "record": record.to_dict()}


# =====================================================================
# 通知信函 / 悼文 / 评分路由（需要认证）
# =====================================================================


@app.get("/api/letters/types", tags=["letters"])
async def letters_types(user: dict = Depends(get_current_user)):
    """GET /api/letters/types - 列出 8 种信函类型"""
    from ..notification_letters.models import DEFAULT_DISCLAIMER
    from ..notification_letters.templates import LETTER_TYPES

    return {
        "types": [dict(t) for t in LETTER_TYPES],
        "count": len(LETTER_TYPES),
        "disclaimer": DEFAULT_DISCLAIMER,
    }


@app.get("/api/letters/template", tags=["letters"])
async def letters_template(
    type: str = Query(default=""), user: dict = Depends(get_current_user)
):
    """GET /api/letters/template?type=xxx - 返回原始模板"""
    from ..notification_letters.models import DEFAULT_DISCLAIMER
    from ..notification_letters.templates import (
        LETTER_TEMPLATES,
        LETTER_TYPES,
        get_letter_type_meta,
    )

    letter_type = (type or "").strip()
    if not letter_type:
        raise HTTPException(
            status_code=400,
            detail={"error": "缺少 type 参数", "disclaimer": DEFAULT_DISCLAIMER},
        )
    if letter_type not in LETTER_TEMPLATES:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"未知信函类型: {letter_type}",
                "supported_types": [t["type"] for t in LETTER_TYPES],
                "disclaimer": DEFAULT_DISCLAIMER,
            },
        )
    meta = get_letter_type_meta(letter_type) or {}
    return {
        "type": letter_type,
        "name": meta.get("name", ""),
        "template": LETTER_TEMPLATES[letter_type],
        "extra_fields_needed": meta.get("extra_fields_needed", []),
        "disclaimer": DEFAULT_DISCLAIMER,
    }


class LetterGenerateRequest(BaseModel):
    letter_type: str
    decedent_name: str = ""
    decedent_id_masked: str = ""
    death_date: str = ""
    applicant_name: str = ""
    applicant_relationship: str = ""
    recipient_org: str = ""
    extra_fields: dict[str, Any] | None = None
    language: str = "zh-CN"
    use_llm: bool = False


@app.post("/api/letters/generate", tags=["letters"])
async def letters_generate(req: LetterGenerateRequest, user: dict = Depends(get_current_user)):
    """POST /api/letters/generate - 生成通知信函"""
    from ..notification_letters import LetterGenerator, LetterRequest
    from ..notification_letters.models import DEFAULT_DISCLAIMER

    try:
        request = LetterRequest(
            letter_type=req.letter_type,
            decedent_name=req.decedent_name,
            decedent_id_masked=req.decedent_id_masked,
            death_date=req.death_date,
            applicant_name=req.applicant_name,
            applicant_relationship=req.applicant_relationship,
            recipient_org=req.recipient_org,
            extra_fields=req.extra_fields or {},
            language=req.language,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": f"请求参数无效: {exc}", "disclaimer": DEFAULT_DISCLAIMER},
        ) from exc
    generator = LetterGenerator(use_llm=req.use_llm)
    try:
        result = generator.generate(request)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "disclaimer": DEFAULT_DISCLAIMER},
        ) from exc
    return result.to_dict()


_PLAN_SCORE_DISCLAIMER = (
    "评分仅反映信息完整度，不代表法律效力；"
    "建议结合律师/公证处专业意见。"
)


@app.get("/api/plan-score", tags=["plan-score"])
async def plan_score(user: dict = Depends(get_current_user)):
    """GET /api/plan-score - 获取当前用户的规划完整度评分"""
    from ..plan_score.scorer import PlanScorer

    scorer = PlanScorer()
    result = scorer.score(user["user_id"])
    payload = result.to_dict()
    payload["disclaimer"] = _PLAN_SCORE_DISCLAIMER
    return payload


@app.get("/api/plan-score/detail", tags=["plan-score"])
async def plan_score_detail(user: dict = Depends(get_current_user)):
    """GET /api/plan-score/detail - 获取详细分解"""
    from ..plan_score.scorer import PlanScorer

    scorer = PlanScorer()
    result = scorer.score(user["user_id"])
    payload = result.to_dict()
    payload["disclaimer"] = _PLAN_SCORE_DISCLAIMER
    return payload


_MEMORIAL_DISCLAIMER = "AI 生成的悼文仅供参考，建议家属审阅修改后使用。"


@app.get("/api/memorial/types", tags=["memorial"])
async def memorial_types(user: dict = Depends(get_current_user)):
    """GET /api/memorial/types - 列出 5 种悼文文档类型"""
    from ..memorial_writer.models import (
        DOC_TYPES,
        VALID_FAITHS,
        VALID_LANGUAGES,
        VALID_TONES,
    )

    types_list = []
    for key, meta in DOC_TYPES.items():
        word_lo, word_hi = meta["word_range"]
        types_list.append({
            "key": key,
            "name": meta["name"],
            "name_en": meta["name_en"],
            "description": meta["description"],
            "word_range": [word_lo, word_hi],
        })
    return {
        "types": types_list,
        "tones": list(VALID_TONES),
        "faiths": list(VALID_FAITHS),
        "languages": list(VALID_LANGUAGES),
        "disclaimer": _MEMORIAL_DISCLAIMER,
    }


@app.post("/api/memorial/generate", tags=["memorial"])
async def memorial_generate(
    request: Request, user: dict = Depends(get_current_user)
):
    """POST /api/memorial/generate - 生成悼文/讣告/答谢词/墓志铭/追思会致辞

    入参为 MemorialRequest 字段（dict），由 ``MemorialRequest.from_dict`` 解析。
    """
    from ..memorial_writer.generator import MemorialGenerator
    from ..memorial_writer.models import MemorialRequest

    try:
        raw = await request.body()
        req = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": f"invalid json: {exc}", "disclaimer": _MEMORIAL_DISCLAIMER},
        ) from exc
    try:
        memorial_req = MemorialRequest.from_dict(req)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": f"参数解析失败: {exc}", "disclaimer": _MEMORIAL_DISCLAIMER},
        ) from exc
    errors = memorial_req.validate()
    if errors:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "参数校验失败: " + "; ".join(errors),
                "disclaimer": _MEMORIAL_DISCLAIMER,
            },
        )
    try:
        gen = MemorialGenerator()
        result = await gen.generate(memorial_req)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "disclaimer": _MEMORIAL_DISCLAIMER},
        ) from exc
    except Exception as exc:
        logger.exception("memorial generate failed")
        raise HTTPException(
            status_code=500,
            detail={"error": f"server error: {exc}", "disclaimer": _MEMORIAL_DISCLAIMER},
        ) from exc
    payload = result.to_dict()
    payload["disclaimer"] = _MEMORIAL_DISCLAIMER
    return payload


# =====================================================================
# 客服工单 / Onboarding / 技能管理 路由（需要认证）
# =====================================================================

_SUPPORT_DISCLAIMER = (
    "工单系统用于反馈/咨询/投诉，不替代紧急救援；"
    "如有自伤/自杀风险请立即拨打 120 / 110 / 400-161-9995。"
)


@app.get("/api/support/tickets", tags=["support"])
async def support_tickets_list(user: dict = Depends(get_current_user)):
    """GET /api/support/tickets - 列出我的工单"""
    from ..support.store import TicketStore

    store = TicketStore()
    tickets = store.list_user_tickets(user["user_id"])
    return {
        "tickets": [t.to_dict() for t in tickets],
        "count": len(tickets),
        "disclaimer": _SUPPORT_DISCLAIMER,
    }


@app.get("/api/support/tickets/{ticket_id}", tags=["support"])
async def support_ticket_get(ticket_id: str, user: dict = Depends(get_current_user)):
    """GET /api/support/tickets/<id> - 工单详情（含 ownership 校验）"""
    from ..support.store import TicketStore

    store = TicketStore()
    ticket = store.get_ticket(ticket_id, user["user_id"])
    if ticket is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "工单不存在或无权限",
                "ticket_id": ticket_id,
                "disclaimer": _SUPPORT_DISCLAIMER,
            },
        )
    return {"ticket": ticket.to_dict(), "disclaimer": _SUPPORT_DISCLAIMER}


class SupportTicketCreateRequest(BaseModel):
    category: str = "咨询"
    priority: str = "普通"
    subject: str = ""
    description: str = ""


@app.post("/api/support/tickets", tags=["support"], status_code=201)
async def support_ticket_create(
    req: SupportTicketCreateRequest, user: dict = Depends(get_current_user)
):
    """POST /api/support/tickets - 创建工单"""
    from ..support.store import TicketStore

    try:
        store = TicketStore()
        ticket = store.create_ticket(
            user_id=user["user_id"],
            category=req.category,
            priority=req.priority,
            subject=req.subject,
            description=req.description,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "disclaimer": _SUPPORT_DISCLAIMER},
        ) from exc
    except Exception as exc:
        logger.exception("support ticket create failed")
        raise HTTPException(
            status_code=500,
            detail={"error": f"server error: {exc}", "disclaimer": _SUPPORT_DISCLAIMER},
        ) from exc
    return {"ticket": ticket.to_dict(), "disclaimer": _SUPPORT_DISCLAIMER}


class SupportTicketReplyRequest(BaseModel):
    content: str


@app.post("/api/support/tickets/{ticket_id}/replies", tags=["support"])
async def support_ticket_reply(
    ticket_id: str, req: SupportTicketReplyRequest, user: dict = Depends(get_current_user)
):
    """POST /api/support/tickets/<id>/replies - 追加回复（仅 user 角色）"""
    from ..support.store import TicketStore

    content = (req.content or "").strip()
    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error": "缺少 content", "disclaimer": _SUPPORT_DISCLAIMER},
        )
    store = TicketStore()
    reply = store.add_reply(
        ticket_id=ticket_id,
        author="user",
        content=str(content),
        user_id=user["user_id"],
    )
    if reply is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "工单不存在或无权限",
                "ticket_id": ticket_id,
                "disclaimer": _SUPPORT_DISCLAIMER,
            },
        )
    return {"reply": reply.to_dict(), "disclaimer": _SUPPORT_DISCLAIMER}


class SupportTicketStatusRequest(BaseModel):
    status: str


@app.put("/api/support/tickets/{ticket_id}/status", tags=["support"])
async def support_ticket_update_status(
    ticket_id: str,
    req: SupportTicketStatusRequest,
    user: dict = Depends(get_current_user),
):
    """PUT /api/support/tickets/<id>/status - 更新工单状态"""
    from ..support.store import TicketStore

    valid_statuses = {"open", "in_progress", "resolved", "closed"}
    if req.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"无效状态 '{req.status}'，允许值: {', '.join(sorted(valid_statuses))}",
                "disclaimer": _SUPPORT_DISCLAIMER,
            },
        )
    store = TicketStore()
    ok = store.update_status(
        ticket_id=ticket_id,
        status=req.status,
        user_id=user["user_id"],
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "工单不存在、无权限或状态流转不合法",
                "ticket_id": ticket_id,
                "disclaimer": _SUPPORT_DISCLAIMER,
            },
        )
    ticket = store.get_ticket(ticket_id, user["user_id"])
    return {
        "ticket": ticket.to_dict() if ticket else {"id": ticket_id, "status": req.status},
        "disclaimer": _SUPPORT_DISCLAIMER,
    }


_ONBOARDING_DISCLAIMER = (
    "Onboarding 画像用于个性化引导，可随时通过重新引导修改；"
    "如不再使用本平台，可在帮助中心申请数据删除。"
)


@app.get("/api/onboarding", tags=["onboarding"])
async def onboarding_get(user: dict = Depends(get_current_user)):
    """GET /api/onboarding - 返回当前用户 onboarding 画像（无则 null）"""
    from ..onboarding.store import OnboardingStore

    store = OnboardingStore()
    profile = store.load(user["user_id"])
    if profile is None:
        return {
            "profile": None,
            "completed": False,
            "disclaimer": _ONBOARDING_DISCLAIMER,
        }
    return {
        "profile": profile.to_dict(),
        "completed": True,
        "disclaimer": _ONBOARDING_DISCLAIMER,
    }


@app.get("/api/onboarding/step/{step}", tags=["onboarding"])
async def onboarding_step(step: int):
    """GET /api/onboarding/step/<index> - 返回第 N 步问题（未认证也允许）"""
    from ..onboarding.wizard import OnboardingWizard

    wiz = OnboardingWizard()
    try:
        step_data = wiz.get_step(step)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "step": step_data,
        "total_steps": wiz.TOTAL_STEPS,
        "disclaimer": _ONBOARDING_DISCLAIMER,
    }


@app.post("/api/onboarding", tags=["onboarding"])
async def onboarding_save(request: Request, user: dict = Depends(get_current_user)):
    """POST /api/onboarding - 保存 onboarding 画像"""
    from ..onboarding.store import OnboardingStore
    from ..onboarding.wizard import OnboardingWizard

    try:
        raw = await request.body()
        req = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": f"invalid json: {exc}", "disclaimer": _ONBOARDING_DISCLAIMER},
        ) from exc
    store = OnboardingStore()
    wiz = OnboardingWizard(store=store)
    try:
        profile = wiz.save_profile(user["user_id"], req)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "disclaimer": _ONBOARDING_DISCLAIMER},
        ) from exc
    except Exception as exc:
        logger.exception("onboarding save failed")
        raise HTTPException(
            status_code=500,
            detail={"error": f"server error: {exc}", "disclaimer": _ONBOARDING_DISCLAIMER},
        ) from exc
    return {
        "profile": profile.to_dict(),
        "user_profile": OnboardingWizard.to_user_profile(profile),
        "completed": True,
        "disclaimer": _ONBOARDING_DISCLAIMER,
    }


@app.delete("/api/onboarding", tags=["onboarding"])
async def onboarding_delete(user: dict = Depends(get_current_user)):
    """DELETE /api/onboarding - 删除 onboarding 画像"""
    from ..onboarding.store import OnboardingStore

    store = OnboardingStore()
    ok = store.delete(user["user_id"])
    if ok:
        return {"deleted": True, "disclaimer": _ONBOARDING_DISCLAIMER}
    return JSONResponse(
        status_code=404,
        content={"error": "onboarding 画像不存在", "disclaimer": _ONBOARDING_DISCLAIMER},
    )


# ---------- 技能管理 ----------


@app.get("/api/skills", tags=["skills"])
async def skills_list(user: dict = Depends(get_current_user)):
    """GET /api/skills - 列出所有技能"""
    try:
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        skills = mgr.list_skills()
        return {"skills": skills, "count": len(skills)}
    except Exception as exc:
        logger.exception("skills list failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/skills/{skill_name}", tags=["skills"])
async def skill_get(skill_name: str, user: dict = Depends(get_current_user)):
    """GET /api/skills/<name> - 获取技能详情"""
    try:
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        skill = mgr.get_skill(skill_name)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 不存在")
        return {"skill": skill}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("skill get failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class SkillCreateRequest(BaseModel):
    name: str
    description: str
    content: str
    version: str = "1.0"


@app.post("/api/skills", tags=["skills"], status_code=201)
async def skill_create(req: SkillCreateRequest, user: dict = Depends(get_current_user)):
    """POST /api/skills - 创建新技能"""
    try:
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        skill = mgr.create_skill(
            name=req.name,
            description=req.description,
            content=req.content,
            version=req.version,
        )
        return {"ok": True, "skill": skill}
    except Exception as exc:
        logger.exception("skill create failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class SkillImportRequest(BaseModel):
    url: str


@app.post("/api/skills/import", tags=["skills"], status_code=201)
async def skill_import(req: SkillImportRequest, user: dict = Depends(get_current_user)):
    """POST /api/skills/import - 从 URL 导入技能"""
    try:
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        skill = mgr.import_skill_from_url(req.url)
        return {"ok": True, "skill": skill}
    except Exception as exc:
        logger.exception("skill import failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class SkillGenerateRequest(BaseModel):
    prompt: str
    name: str


@app.post("/api/skills/generate", tags=["skills"], status_code=201)
async def skill_generate(req: SkillGenerateRequest, user: dict = Depends(get_current_user)):
    """POST /api/skills/generate - AI 生成技能"""
    try:
        from ..llm import llm_client

        if not llm_client.api_key:
            raise HTTPException(
                status_code=503,
                detail="LLM 未配置，无法生成技能。请先设置 LLM API key。",
            )
        system_prompt = (
            "你是一个 SKILL.md 技能文件生成器。"
            "根据用户的描述生成一个符合 SKILL.md 格式的 Markdown 内容。"
            "SKILL.md 格式要求：包含 YAML frontmatter（name, description, version）"
            "和 Markdown body（技能指令和说明）。"
            "只输出 SKILL.md 的 body 部分（不包含 frontmatter），"
            "frontmatter 会由系统自动添加。"
            "输出应该清晰、结构化、可操作。"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": req.prompt},
        ]
        generated_content = await llm_client.chat(messages, temperature=0.7)
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        skill = mgr.create_skill(
            name=req.name,
            description=f"AI 生成: {req.prompt[:100]}",
            content=generated_content,
            version="1.0",
        )
        return {"ok": True, "skill": skill}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("skill generate failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.delete("/api/skills/{skill_name}", tags=["skills"])
async def skill_delete(skill_name: str, user: dict = Depends(get_current_user)):
    """DELETE /api/skills/<name> - 删除技能"""
    try:
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        mgr.delete_skill(skill_name)
        return {"ok": True}
    except Exception as exc:
        logger.exception("skill delete failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class SkillInvokeRequest(BaseModel):
    query: str


@app.post("/api/skills/{skill_name}/invoke", tags=["skills"])
async def skill_invoke(
    skill_name: str, req: SkillInvokeRequest, user: dict = Depends(get_current_user)
):
    """POST /api/skills/<name>/invoke - 测试/调用技能"""
    try:
        from ..marketplace.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        result = mgr.invoke_skill(skill_name, req.query)
        return {"result": result}
    except Exception as exc:
        logger.exception("skill invoke failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


# =====================================================================
# Billing / Marketplace / Compliance / i18n / Alignment / Governance / Multimodal
# =====================================================================


@app.get("/api/billing/status", tags=["billing"])
async def billing_status(user: dict | None = Depends(get_optional_user)):
    """GET /api/billing/status - 返回计费状态（订阅 + 计量概览）"""
    try:
        from ..billing import get_subscription_manager
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("billing"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "billing module is disabled (DEADMAN_BILLING_ENABLED=0)",
                },
            )
        sub_mgr = get_subscription_manager()
        user_id = user["user_id"] if user else "anonymous"
        sub = sub_mgr.get_current(user_id)
        return {
            "enabled": True,
            "subscription": sub.to_dict() if sub else None,
            "is_active": sub.is_active() if sub else False,
            "plan_name": sub.plan_name if sub else "free",
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"billing module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("billing status failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/billing/usage", tags=["billing"])
async def billing_usage(
    user: dict | None = Depends(get_optional_user),
    user_id_q: str | None = Query(default=None, alias="user_id"),
    period: str | None = Query(default=None),
):
    """GET /api/billing/usage - 返回使用量"""
    try:
        from ..billing import get_usage_tracker
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("billing"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "billing module is disabled (DEADMAN_BILLING_ENABLED=0)",
                },
            )
        tracker = get_usage_tracker()
        user_id = user["user_id"] if user else (user_id_q or "anonymous")
        report = tracker.get_usage(user_id, period)
        return {
            "enabled": True,
            "user_id": user_id,
            "period": report.period,
            "usage": {
                "llm_tokens": report.llm_tokens,
                "tool_calls": report.tool_calls,
                "storage_mb": report.storage_mb,
                "multimodal_calls": report.multimodal_calls,
                "by_model": report.by_model,
                "by_tool": report.by_tool,
                "by_multimodal_type": report.by_multimodal_type,
            },
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"billing module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("billing usage failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/billing/plans", tags=["billing"])
async def billing_plans():
    """GET /api/billing/plans - 返回可用计划列表"""
    try:
        from ..billing.plans import list_plans
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("billing"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "billing module is disabled (DEADMAN_BILLING_ENABLED=0)",
                },
            )
        plans = list_plans()
        return {
            "enabled": True,
            "plans": [
                {
                    "name": p.name.value,
                    "display_name": p.display_name,
                    "price_monthly": p.price_monthly,
                    "price_yearly": p.price_yearly,
                    "sla_level": p.sla_level,
                    "support_level": p.support_level,
                    "data_retention_days": p.data_retention_days,
                    "description": p.description,
                    "limits": {
                        "llm_tokens_daily": p.limits.llm_tokens_daily,
                        "llm_tokens_monthly": p.limits.llm_tokens_monthly,
                        "tool_calls_daily": p.limits.tool_calls_daily,
                        "tool_calls_monthly": p.limits.tool_calls_monthly,
                        "storage_mb": p.limits.storage_mb,
                        "multimodal_calls_daily": p.limits.multimodal_calls_daily,
                        "multimodal_calls_monthly": p.limits.multimodal_calls_monthly,
                    },
                    "features": list(p.features),
                }
                for p in plans
            ],
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"billing module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("billing plans failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


class BillingSubscribeRequest(BaseModel):
    plan_name: str
    billing_cycle: str = "monthly"
    with_trial: bool = False


@app.post("/api/billing/subscribe", tags=["billing"], status_code=201)
async def billing_subscribe(
    req: BillingSubscribeRequest, user: dict = Depends(get_current_user)
):
    """POST /api/billing/subscribe - 订阅计划"""
    try:
        from ..billing import get_subscription_manager
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("billing"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "billing module is disabled (DEADMAN_BILLING_ENABLED=0)",
                },
            )
        sub_mgr = get_subscription_manager()
        sub = sub_mgr.subscribe(
            user_id=user["user_id"],
            plan_name=req.plan_name,
            billing_cycle=req.billing_cycle,
            with_trial=req.with_trial,
        )
        return {
            "ok": True,
            "subscription": sub.to_dict(),
            "is_active": sub.is_active(),
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"billing module unavailable: {exc}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("billing subscribe failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/marketplace/skills", tags=["marketplace"])
async def marketplace_skills(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    sort: str = Query(default="newest"),
):
    """GET /api/marketplace/skills - 返回市场技能列表"""
    try:
        from ..infrastructure.feature_flags import is_enabled
        from ..marketplace import get_marketplace_registry

        if not is_enabled("marketplace"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "marketplace module is disabled (DEADMAN_MARKETPLACE_ENABLED=0)",
                },
            )
        registry = get_marketplace_registry()
        listings = registry.list(query=q, category=category, sort_by=sort)
        return {
            "enabled": True,
            "skills": [listing.to_dict() for listing in listings],
            "count": len(listings),
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"marketplace module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        if "disabled" in str(exc).lower() or "MarketplaceError" in type(exc).__name__:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        logger.exception("marketplace skills failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/compliance/status", tags=["compliance"])
async def compliance_status(user: dict | None = Depends(get_optional_user)):
    """GET /api/compliance/status - 返回合规状态"""
    try:
        from ..compliance import get_audit_reporter, get_consent_manager
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("compliance"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "compliance module is disabled (DEADMAN_COMPLIANCE_ENABLED=0)",
                },
            )
        consent_mgr = get_consent_manager()
        audit_reporter = get_audit_reporter()
        user_id = user["user_id"] if user else "anonymous"
        consents = consent_mgr.list_user_consents(user_id)
        reports = audit_reporter.list_reports(limit=5)
        return {
            "enabled": True,
            "user_consents": {
                k: v.value if hasattr(v, "value") else str(v)
                for k, v in consents.items()
            },
            "recent_reports": [r.to_dict() for r in reports],
            "report_count": len(reports),
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"compliance module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("compliance status failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/i18n/messages", tags=["i18n"])
async def i18n_messages(locale: str = Query(default="zh-CN")):
    """GET /api/i18n/messages - 返回多语言消息"""
    try:
        from ..i18n import Locale, get_message_bundle
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("i18n"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "i18n module is disabled (DEADMAN_I18N_ENABLED=0)",
                },
            )
        bundle = get_message_bundle()
        loc = Locale.from_string(locale)
        keys = bundle.list_keys(loc)
        messages = {k: bundle.get(k, loc) for k in keys}
        return {
            "enabled": True,
            "locale": loc.value,
            "messages": messages,
            "key_count": len(messages),
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"i18n module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("i18n messages failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/i18n/currency", tags=["i18n"])
async def i18n_currency():
    """GET /api/i18n/currency - 返回货币信息与汇率"""
    try:
        from ..i18n import Currency, get_currency_converter
        from ..infrastructure.feature_flags import is_enabled

        if not is_enabled("i18n"):
            return JSONResponse(
                status_code=503,
                content={
                    "enabled": False,
                    "error": "i18n module is disabled (DEADMAN_I18N_ENABLED=0)",
                },
            )
        converter = get_currency_converter()
        rates = converter.get_all_rates()
        currencies = [
            {
                "code": c.value,
                "symbol": c.symbol,
                "is_zero_decimal": c.is_zero_decimal,
                "default_locale": c.default_locale.value,
            }
            for c in Currency
        ]
        return {
            "enabled": True,
            "base": "CNY",
            "rates": rates,
            "currencies": currencies,
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail=f"i18n module unavailable: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("i18n currency failed")
        raise HTTPException(status_code=500, detail=f"server error: {exc}") from exc


@app.get("/api/alignment/status", tags=["alignment"])
async def alignment_status():
    """GET /api/alignment/status - Alignment 对齐训练状态"""
    try:
        from ..alignment import AlignmentDisabledError, get_alignment_manager

        try:
            mgr = get_alignment_manager()
        except AlignmentDisabledError:
            return {
                "enabled": False,
                "message": "Alignment 模块未启用 (DEADMAN_ALIGNMENT_ENABLED=0)",
            }
        return {"enabled": True, "stats": mgr.stats()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/governance/status", tags=["governance"])
async def governance_status():
    """GET /api/governance/status - Governance 治理框架状态"""
    try:
        from ..governance import GovernanceDisabledError, get_governance_manager

        try:
            gm = get_governance_manager()
        except GovernanceDisabledError:
            return {
                "enabled": False,
                "message": "Governance 模块未启用 (DEADMAN_GOVERNANCE_ENABLED=0)",
                "redline_enforced": True,
            }
        return {
            "enabled": True,
            "decision_count": gm._decision_count,
            "ai_decision_count": gm._ai_decision_count,
            "human_review_count": gm._human_review_count,
            "bias_incidents": gm._bias_incidents,
            "model_usage": gm._model_usage,
            "user_feedback": gm._user_feedback,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/multimodal/status", tags=["multimodal"])
async def multimodal_status():
    """GET /api/multimodal/status - Multimodal 多模态管道状态"""
    try:
        from ..multimodal import MultimodalDisabledError, get_multimodal_pipeline

        try:
            pipe = get_multimodal_pipeline()
        except MultimodalDisabledError:
            return {
                "enabled": False,
                "message": "Multimodal 模块未启用 (DEADMAN_MULTIMODAL_ENABLED=0)",
            }
        caps = pipe.list_capabilities()
        cfg = pipe.config
        audit = pipe.get_audit_log(limit=10)
        return {
            "enabled": pipe.is_enabled(),
            "capabilities": caps,
            "config": {
                "default_provider": cfg.default_provider,
                "budget_token_per_session": cfg.budget_token_per_session,
                "audit_log_enabled": cfg.audit_log_enabled,
                "pii_redact_ocr": cfg.pii_redact_ocr,
            },
            "recent_audit": list(audit),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# =====================================================================
# 启动入口
# =====================================================================


def main() -> None:
    """命令行入口：启动 FastAPI Web Server（uvicorn）"""
    import argparse

    from ..logging_config import setup_logging as _setup_structlog_logging

    _setup_structlog_logging()

    parser = argparse.ArgumentParser(
        prog="deadman-web-server-fastapi", description="AG-UI Web Server (FastAPI)"
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _setup_structlog_logging(level=args.log_level)

    host = args.host or settings.mcp_server_host
    port = args.port or int(os.getenv("WEB_SERVER_PORT", "8002"))

    import uvicorn

    uvicorn.run(
        "deadman.web.app:app",
        host=host,
        port=port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
