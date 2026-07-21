"""AG-UI Web Server - 提供对话界面 + chat API + SSE 流式 + 运维 API

端点：
  GET  /                   -> 对话界面（index.html）
  GET  /api/health         -> 健康检查
  POST /api/chat           -> 同步对话（返回完整响应）
  GET  /api/stream?query=  -> SSE 流式对话（逐 token 推送）
  GET  /api/agents         -> 智能体列表
  GET  /api/tools          -> MCP 工具列表
  GET  /metrics            -> Prometheus 指标

  --- 运维 API（覆盖 13 领域四件套） ---
  POST /api/cli/<command>  -> 通用 CLI 代理（subprocess 调用，返回 stdout）
  GET  /api/obs/dashboard  -> 可观测性看板（结构化 JSON）
  GET  /api/llm/health     -> LLM 健康（读 data/llm_health.json）
  GET  /api/memory/state   -> 记忆状态（4 层条目数）
  GET  /api/deploy/check   -> 部署工件校验
  GET  /api/health/all     -> 全领域健康汇总

  --- Phase 10：终活笔记（エンディングノート）+ 家庭共享 ---
  GET    /api/ending-note                  -> 获取我的笔记
  POST   /api/ending-note/section          -> 保存某章节
  GET    /api/ending-note/guide/next       -> 获取下一章引导问题
  POST   /api/ending-note/share            -> 共享给家庭成员
  DELETE /api/ending-note/share            -> 取消共享（?target_user_id=xxx）
  GET    /api/ending-note/shared-with-me   -> 共享给我的笔记
  POST   /api/ending-note/trigger          -> 触发投递
  GET    /api/ending-note/completion       -> 填写完整度

  认证：Phase 8 auth 模块未上线，开发期用 user_id query/body 参数降级

前端：web/static/index.html（多页签 SPA，原生 JS，无构建依赖）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import settings

logger = logging.getLogger(__name__)

# 静态文件目录
_STATIC_DIR = Path(__file__).parent / "static"

# 允许通过 /api/cli 代理调用的 CLI 子命令白名单（安全：防止任意命令执行）
# 覆盖全部 13 领域的只读/测试类命令
_CLI_COMMANDS = {
    # 基础
    "version", "eval-list",
    # LLM
    "llm-test", "llm-sync-models", "llm-cost",
    # 提示词
    "prompt-list", "prompt-sync",
    # 规则
    "rule-test", "rule-validate",
    # 智能体
    "agent-list", "agent-ping",
    # 知识库
    "knowledge-list", "knowledge-freshness",
    # MCP 工具
    "tool-list", "mcp-ping",
    # 可观测性
    "obs-dashboard", "obs-test", "obs-export",
    # 记忆
    "memory-list", "memory-test", "memory-ping",
    # A2A
    "a2a-card", "a2a-test", "a2a-registry",
    # 部署
    "deploy-check", "deploy-test",
    # Reflexion
    "reflexion-list", "reflexion-test", "reflexion-ping",
    # 技能
    "skill-list", "skill-validate",
}


class WebServer:
    """AG-UI Web Server

    提供对话界面和 API 端点，与 MCP Server / A2A Server 共存。
    """

    def __init__(self) -> None:
        self.host = settings.mcp_server_host
        # Web UI 端口默认比 MCP +2（MCP=8000, A2A=8001, Web=8002）
        self.port = int(os.getenv("WEB_SERVER_PORT", "8002"))

    def run(self, host: str | None = None, port: int | None = None) -> None:
        host = host or self.host
        port = port or self.port
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug("Web %s - %s", self.address_string(), format % args)

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_file(self, file_path: Path, content_type: str) -> None:
                if not file_path.exists():
                    self.send_error(404, "Not Found")
                    return
                body = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _get_headers_dict(self) -> dict[str, str]:
                """把 BaseHTTPRequestHandler.headers 转为 dict（用于 _require_auth）"""
                return {k.lower(): v for k, v in self.headers.items()}

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if path == "/" or path == "/index.html":
                    self._send_file(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
                elif path == "/api/health":
                    self._send_json(200, {"status": "ok", "service": "ag-ui"})
                elif path == "/api/whoami":
                    self._send_json(200, server_ref._handle_whoami())
                elif path == "/api/stream":
                    self._handle_stream(query)
                elif path == "/api/agents":
                    self._handle_agents()
                elif path == "/api/tools":
                    self._handle_tools()
                elif path == "/api/obs/dashboard":
                    self._handle_obs_dashboard()
                elif path == "/api/llm/health":
                    self._handle_health_file("llm_health.json")
                elif path == "/api/memory/state":
                    self._handle_memory_state()
                elif path == "/api/deploy/check":
                    self._handle_deploy_check()
                elif path == "/api/health/all":
                    self._handle_health_all()
                elif path == "/metrics":
                    self._handle_metrics()
                # === Phase 8: 用户认证（只追加）===
                elif path == "/api/auth/me":
                    headers = self._get_headers_dict()
                    resp = server_ref._handle_auth_me(headers)
                    if resp is None:
                        self._send_json(401, {"error": "未认证或 token 无效"})
                    else:
                        self._send_json(200, resp)
                # === Phase 9: 免责告知 + 热线 + 机构（只追加，不修改其他 Phase 改动）===
                elif path == "/api/disclaimer":
                    self._handle_disclaimer(query)
                elif path == "/api/hotlines":
                    self._handle_hotlines(query)
                elif path == "/api/institutions":
                    self._handle_institutions(query)
                elif path.startswith("/api/institutions/"):
                    institution_id = path[len("/api/institutions/"):]
                    self._handle_institution_by_id(institution_id)
                # === Phase 10: 终活笔记（エンディングノート）+ 家庭共享（只追加）===
                elif path == "/api/ending-note":
                    self._handle_ending_note_get(query)
                elif path == "/api/ending-note/guide/next":
                    self._handle_ending_note_guide_next(query)
                elif path == "/api/ending-note/shared-with-me":
                    self._handle_ending_note_shared_with_me(query)
                elif path == "/api/ending-note/completion":
                    self._handle_ending_note_completion(query)
                # === Phase 11/12/13: 保险库 / 文档提取 / 遗码通（只追加）===
                elif path == "/api/vault/items":
                    self._handle_vault_items_list(query)
                elif path == "/api/vault/beneficiaries":
                    self._handle_vault_beneficiaries()
                elif path == "/api/vault/inherited":
                    self._handle_vault_inherited()
                elif path.startswith("/api/vault/items/"):
                    item_id = path[len("/api/vault/items/"):]
                    self._handle_vault_item_get(item_id)
                elif path == "/api/documents":
                    self._handle_documents_list()
                elif path.startswith("/api/documents/"):
                    doc_id = path[len("/api/documents/"):]
                    self._handle_document_get(doc_id)
                elif path == "/api/cases":
                    self._handle_cases_list()
                elif path.startswith("/api/cases/") and path.endswith("/timeline"):
                    case_id = path[len("/api/cases/"):-len("/timeline")]
                    self._handle_case_timeline(case_id)
                elif path.startswith("/api/cases/"):
                    case_id = path[len("/api/cases/"):]
                    self._handle_case_get(case_id)
                # === Phase 15: Dead Man Switch GET 路由（只追加）===
                elif path == "/api/switch/status":
                    self._handle_switch_status()
                elif path == "/api/switch/actions":
                    self._handle_switch_list_actions()
                # === Phase 15: 通知信函生成器 GET 路由（只追加）===
                elif path == "/api/letters/types":
                    self._handle_letters_types()
                elif path == "/api/letters/template":
                    self._handle_letters_template(query)
                # === Phase 15: plan_score 规划完整度评分 GET 路由（只追加）===
                elif path == "/api/plan-score":
                    self._handle_plan_score()
                elif path == "/api/plan-score/detail":
                    self._handle_plan_score_detail()
                # === Phase 15 (Memorial Writer): AI 悼文撰写 GET 路由（只追加）===
                elif path == "/api/memorial/types":
                    self._handle_memorial_types()
                # === Phase 16C: 合规页面 + 客服工单 + Onboarding GET 路由（只追加）===
                elif path == "/privacy":
                    self._handle_docs_page("privacy")
                elif path == "/terms":
                    self._handle_docs_page("terms")
                elif path == "/support":
                    self._handle_docs_page("support")
                elif path == "/api/support/tickets":
                    self._handle_support_tickets_list()
                elif path.startswith("/api/support/tickets/"):
                    ticket_id = path[len("/api/support/tickets/"):]
                    self._handle_support_ticket_get(ticket_id)
                elif path == "/api/onboarding":
                    self._handle_onboarding_get()
                elif path.startswith("/api/onboarding/step/"):
                    step_str = path[len("/api/onboarding/step/"):]
                    self._handle_onboarding_step(step_str)
                else:
                    # 静态文件（CSS/JS）
                    static_file = _STATIC_DIR / path.lstrip("/")
                    if static_file.exists() and static_file.is_file():
                        ct = "application/octet-stream"
                        if path.endswith(".css"):
                            ct = "text/css"
                        elif path.endswith(".js"):
                            ct = "application/javascript"
                        self._send_file(static_file, ct)
                    else:
                        self.send_error(404, "Not Found")

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/api/auth/register":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        req = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        self._send_json(400, {"error": f"invalid json: {exc}"})
                        return
                    try:
                        resp = asyncio.run(server_ref._handle_auth_register(req))
                        self._send_json(200, resp)
                    except ValueError as exc:
                        # 业务校验失败（邮箱已注册/密码太短等）
                        self._send_json(400, {"error": str(exc)})
                    except Exception as exc:
                        logger.exception("auth register failed")
                        self._send_json(500, {"error": f"server error: {exc}"})
                elif path == "/api/auth/login":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        req = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        self._send_json(400, {"error": f"invalid json: {exc}"})
                        return
                    try:
                        resp = asyncio.run(server_ref._handle_auth_login(req))
                        if resp is None:
                            # 防枚举：不区分"邮箱不存在" vs "密码错"
                            self._send_json(401, {"error": "邮箱或密码错误"})
                        else:
                            self._send_json(200, resp)
                    except Exception as exc:
                        logger.exception("auth login failed")
                        self._send_json(500, {"error": f"server error: {exc}"})
                elif path == "/api/auth/refresh":
                    headers = self._get_headers_dict()
                    resp = asyncio.run(server_ref._handle_auth_refresh(headers))
                    if resp is None:
                        self._send_json(401, {"error": "token 无效或无需刷新"})
                    else:
                        self._send_json(200, resp)
                elif path == "/api/chat":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        req = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        self._send_json(400, {"error": f"invalid json: {exc}"})
                        return
                    query_text = req.get("query", "")
                    agent = req.get("agent", "death-aftercare")
                    history = req.get("history", [])
                    # 优先用认证用户（如果 token 有效），否则降级 anonymous
                    headers = self._get_headers_dict()
                    user = server_ref._require_auth(headers)
                    if user is not None:
                        req.setdefault("user_id", user.get("user_id"))
                        req.setdefault("user_email", user.get("email"))
                    user_id = req.get("user_id") or req.get("userId") or None
                    resp = asyncio.run(
                        server_ref._handle_chat(agent, query_text, history, user_id)
                    )
                    self._send_json(200, resp)
                elif path == "/api/whoami":
                    self._send_json(200, server_ref._handle_whoami())
                elif path.startswith("/api/cli/"):
                    command = path[len("/api/cli/"):]
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        req = json.loads(raw.decode("utf-8")) if raw else {}
                    except json.JSONDecodeError:
                        req = {}
                    resp = server_ref._handle_cli(command, req)
                    self._send_json(200, resp)
                # === Phase 10: 终活笔记 POST 路由（只追加）===
                elif path == "/api/ending-note/section":
                    self._handle_ending_note_section()
                elif path == "/api/ending-note/share":
                    self._handle_ending_note_share()
                elif path == "/api/ending-note/trigger":
                    self._handle_ending_note_trigger()
                # === Phase 11/12/13: 保险库 / 文档提取 / 遗码通 POST 路由（只追加）===
                elif path == "/api/vault/items":
                    self._handle_vault_item_add()
                elif path == "/api/documents/extract":
                    self._handle_document_extract()
                elif path == "/api/cases":
                    self._handle_case_create()
                elif path.startswith("/api/vault/items/") and path.endswith("/trigger"):
                    item_id = path[len("/api/vault/items/"):-len("/trigger")]
                    self._handle_vault_item_trigger(item_id)
                elif path.startswith("/api/cases/") and path.endswith("/events"):
                    case_id = path[len("/api/cases/"):-len("/events")]
                    self._handle_case_event_add(case_id)
                elif path.startswith("/api/cases/") and path.endswith("/archive"):
                    case_id = path[len("/api/cases/"):-len("/archive")]
                    self._handle_case_archive(case_id)
                # === Phase 15: Dead Man Switch POST 路由（只追加）===
                elif path == "/api/switch/init":
                    self._handle_switch_init()
                elif path == "/api/switch/checkin":
                    self._handle_switch_checkin()
                elif path == "/api/switch/tick":
                    self._handle_switch_tick()
                elif path == "/api/switch/verify-contact":
                    self._handle_switch_verify_contact()
                elif path == "/api/switch/verify-heir":
                    self._handle_switch_verify_heir()
                elif path == "/api/switch/cancel":
                    self._handle_switch_cancel()
                elif path == "/api/switch/execute":
                    self._handle_switch_execute()
                # === Phase 15: 通知信函生成器 POST 路由（只追加）===
                elif path == "/api/letters/generate":
                    self._handle_letters_generate()
                # === Phase 15 (Memorial Writer): AI 悼文撰写 POST 路由（只追加）===
                elif path == "/api/memorial/generate":
                    self._handle_memorial_generate()
                # === Phase 16C: 客服工单 + Onboarding POST 路由（只追加）===
                elif path == "/api/support/tickets":
                    self._handle_support_ticket_create()
                elif path.startswith("/api/support/tickets/") and path.endswith("/replies"):
                    ticket_id = path[len("/api/support/tickets/"):-len("/replies")]
                    self._handle_support_ticket_reply(ticket_id)
                elif path == "/api/onboarding":
                    self._handle_onboarding_save()
                else:
                    self.send_error(404, "Not Found")

            def do_DELETE(self) -> None:  # noqa: N802
                """Phase 10/11/12 新增：DELETE 路由"""
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/api/ending-note/share":
                    self._handle_ending_note_unshare(query)
                # === Phase 11/12: 保险库 / 文档提取 DELETE 路由（只追加）===
                elif path.startswith("/api/vault/items/"):
                    item_id = path[len("/api/vault/items/"):]
                    self._handle_vault_item_delete(item_id)
                elif path.startswith("/api/documents/"):
                    doc_id = path[len("/api/documents/"):]
                    self._handle_document_delete(doc_id)
                else:
                    self.send_error(404, "Not Found")

            def _handle_stream(self, query: dict[str, list[str]]) -> None:
                """SSE 流式对话 - Phase 14 后走完整 graph 规则链"""
                q = query.get("query", [""])[0]
                agent = query.get("agent", ["death-aftercare"])[0]
                # Phase 14：从 Authorization 头解析用户（与 /api/chat 一致）
                # 未认证降级为 anonymous，不阻塞流式
                headers = self._get_headers_dict()
                user_info = server_ref._require_auth(headers)
                stream_user_id = user_info["user_id"] if user_info else None
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    asyncio.run(
                        server_ref._stream_chat(self.wfile, q, agent, stream_user_id)
                    )
                except Exception as exc:
                    logger.warning("SSE 流式失败: %s", exc)
                    self.wfile.write(
                        f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode()
                    )
                    self.wfile.flush()

            def _handle_agents(self) -> None:
                """返回智能体列表"""
                agents = [
                    {"id": "death-aftercare", "name": "身后事流程引导员"},
                    {"id": "legal-advisor", "name": "法律咨询智能体"},
                    {"id": "financial-analyst", "name": "财务分析智能体"},
                    {"id": "policy-researcher", "name": "政策研究智能体"},
                    {"id": "cross-border-specialist", "name": "跨境事务智能体"},
                    {"id": "medical-guide", "name": "医疗导航智能体"},
                ]
                self._send_json(200, {"agents": agents})

            def _handle_tools(self) -> None:
                """返回 MCP 工具列表"""
                try:
                    from ..mcp_server.server import mcp
                    self._send_json(200, {"tools": mcp.list_tools()})
                except Exception as exc:
                    self._send_json(200, {"tools": [], "error": str(exc)})

            def _handle_metrics(self) -> None:
                """Prometheus 指标端点"""
                try:
                    from ..observability.metrics import metrics_collector
                    text = metrics_collector.export_prometheus()
                    body = text.encode("utf-8")
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})

            def _handle_obs_dashboard(self) -> None:
                """可观测性看板（结构化 JSON）"""
                try:
                    from ..observability import metrics_collector
                    self._send_json(200, metrics_collector.get_dashboard())
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})

            def _handle_health_file(self, filename: str) -> None:
                """读取 data/<filename> 健康文件"""
                data_file = settings.project_root / "data" / filename
                if data_file.exists():
                    try:
                        data = json.loads(data_file.read_text(encoding="utf-8"))
                        self._send_json(200, data)
                        return
                    except Exception as exc:
                        self._send_json(500, {"error": f"读取失败: {exc}"})
                        return
                self._send_json(200, {"status": "no_data", "message": f"{filename} 尚未生成，请先运行对应 CLI 命令"})

            def _handle_memory_state(self) -> None:
                """记忆 4 层状态"""
                try:
                    from ..memory.manager import MemoryManager
                    mgr = MemoryManager()
                    self._send_json(200, {
                        "working": len(mgr.working._turns) if hasattr(mgr.working, "_turns") else 0,
                        "episodic": len(mgr.episodic._store),
                        "semantic": len(mgr.semantic.facts),
                        "semantic_profiles": len(mgr.semantic.user_profiles),
                        "semantic_contradictions": len(mgr.semantic.pending_contradictions),
                        "procedural": len(mgr.procedural._procedures) if hasattr(mgr.procedural, "_procedures") else 0,
                        "graphiti_enabled": mgr.graphiti is not None,
                        "lightrag_enabled": mgr.lightrag is not None,
                    })
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})

            def _handle_deploy_check(self) -> None:
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
                results = []
                for name, path in artifacts:
                    results.append({"name": name, "exists": path.exists(), "path": str(path)})
                # compose 语法
                compose_path = project_root / "docker-compose.yml"
                compose_ok = False
                services = []
                if compose_path.exists():
                    try:
                        with open(compose_path, encoding="utf-8") as f:
                            compose = yaml.safe_load(f) or {}
                        services = list((compose.get("services") or {}).keys())
                        compose_ok = True
                    except Exception:
                        pass
                self._send_json(200, {
                    "artifacts": results,
                    "compose_valid": compose_ok,
                    "compose_services": services,
                })

            def _handle_health_all(self) -> None:
                """全领域健康汇总（读取所有 data/*_health.json）"""
                data_dir = settings.project_root / "data"
                domains = [
                    "llm", "prompt", "rule", "agent", "knowledge",
                    "eval", "tool", "mcp", "obs", "memory",
                    "a2a", "deploy", "reflexion", "skill",
                ]
                summary = {}
                for domain in domains:
                    hf = data_dir / f"{domain}_health.json"
                    if hf.exists():
                        try:
                            summary[domain] = json.loads(hf.read_text(encoding="utf-8"))
                        except Exception:
                            summary[domain] = {"status": "parse_error"}
                    else:
                        summary[domain] = {"status": "no_data"}
                self._send_json(200, summary)

            # === Phase 9: 免责告知 + 热线 + 机构 ===

            @staticmethod
            def _disclaimer_footer() -> str:
                """所有 Phase 9 响应附带的 disclaimer 字段（transparency-framework）"""
                from deadman.disclaimer.text import DisclaimerBuilder
                return DisclaimerBuilder.for_web_footer()

            def _handle_disclaimer(self, query: dict[str, list[str]]) -> None:
                """GET /api/disclaimer - 返回免责告知

                无参数：完整开场告知
                ?scenario=legal|agent|data|identity：场景化简短提醒
                ?format=footer：Web 页面底部固定告知
                """
                from deadman.disclaimer.text import DisclaimerBuilder
                scenario = query.get("scenario", [None])[0]
                fmt = query.get("format", [None])[0]
                try:
                    if fmt == "footer":
                        text = DisclaimerBuilder.for_web_footer()
                        kind = "footer"
                    elif scenario:
                        text = DisclaimerBuilder.short_reminder(scenario)
                        kind = f"scenario:{scenario}"
                    else:
                        text = DisclaimerBuilder.full_opening()
                        kind = "full_opening"
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {
                    "text": text,
                    "kind": kind,
                    "disclaimer": self._disclaimer_footer(),
                })

            def _handle_hotlines(self, query: dict[str, list[str]]) -> None:
                """GET /api/hotlines?province=&function= - 热线查询"""
                from deadman.hotlines.lookup import HotlineLookup
                province = query.get("province", [None])[0]
                function = query.get("function", [None])[0]
                lookup = HotlineLookup()
                results = lookup.lookup(province, function)
                self._send_json(200, {
                    "hotlines": results,
                    "count": len(results),
                    "query": {"province": province, "function": function},
                    "disclaimer": self._disclaimer_footer(),
                })

            def _handle_institutions(self, query: dict[str, list[str]]) -> None:
                """GET /api/institutions?province=&city=&type=&keyword= - 机构查询"""
                from deadman.institutions.store import InstitutionStore
                province = query.get("province", [None])[0]
                city = query.get("city", [None])[0]
                inst_type = query.get("type", [None])[0]
                keyword = query.get("keyword", [None])[0]
                store = InstitutionStore()
                results = store.search(province, city, inst_type, keyword)
                self._send_json(200, {
                    "institutions": [i.to_dict() for i in results],
                    "count": len(results),
                    "query": {
                        "province": province, "city": city,
                        "type": inst_type, "keyword": keyword,
                    },
                    "disclaimer": self._disclaimer_footer(),
                })

            def _handle_institution_by_id(self, institution_id: str) -> None:
                """GET /api/institutions/<id> - 机构详情"""
                from deadman.institutions.store import InstitutionStore
                store = InstitutionStore()
                inst = store.get(institution_id)
                if inst is None:
                    self._send_json(404, {
                        "error": "机构不存在",
                        "institution_id": institution_id,
                        "disclaimer": self._disclaimer_footer(),
                    })
                    return
                payload = inst.to_dict()
                payload["needs_verification_warning"] = inst.needs_verification_warning()
                payload["disclaimer"] = self._disclaimer_footer()
                self._send_json(200, payload)

            # ============================================================
            # Phase 10: 终活笔记 handlers（只追加，不修改其他 Phase 代码）
            # ============================================================

            @staticmethod
            def _ending_note_disclaimer() -> str:
                """终活笔记响应附带的边界告知

                service-boundary-framework.md 第三章：明确告知"终活笔记不是法律文件"
                """
                return (
                    "终活笔记不是法律文件，不替代遗嘱/信托/医疗预嘱；"
                    "如需法律效力，请咨询律师/公证处办理正式文件。"
                )

            def _ending_note_user_id(self, query: dict[str, list[str]]) -> str | None:
                """从认证上下文取 user_id（Phase 14 P0-gap-2 修复）

                原实现：从 query string 取 ?user_id=xxx，任意登录用户改 query
                即可拉取他人终活笔记（横向越权漏洞）。

                现实现：优先从 Authorization 头解析认证用户；
                query string 的 user_id 仅作开发期降级（且仅当环境变量
                DEADMAN_ALLOW_QUERY_USER_ID=1 时生效），生产环境强制走 auth。
                """
                # 优先走 auth
                user = self._phase_auth_user()
                if user is not None:
                    return user["user_id"]
                # 开发期降级：仅当显式开启时允许 query user_id
                if os.environ.get("DEADMAN_ALLOW_QUERY_USER_ID") == "1":
                    return query.get("user_id", [None])[0]
                return None

            def _handle_ending_note_get(self, query: dict[str, list[str]]) -> None:
                """GET /api/ending-note - 获取我的笔记（Phase 14 后强制认证）"""
                from deadman.ending_note.store import EndingNoteStore
                user_id = self._ending_note_user_id(query)
                if not user_id:
                    self._phase_unauthorized()
                    return
                store = EndingNoteStore()
                note = store.load(user_id)
                if note is None:
                    self._send_json(404, {
                        "note": None,
                        "message": "尚无终活笔记，请调 POST /api/ending-note/section 开始填写",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                self._send_json(200, {
                    "note": note.to_dict(),
                    "disclaimer": self._ending_note_disclaimer(),
                })

            def _handle_ending_note_section(self) -> None:
                """POST /api/ending-note/section - 保存某章节

                body: {
                    section: str,  # personal_info/family_relations/.../will_intent
                    answer: dict   # 用户回答
                }
                user_id 从 Authorization 头解析（Phase 14 P0-gap-2 修复：
                原 body.user_id 字段已废弃，请求体里的 user_id 会被忽略）
                """
                from deadman.ending_note.store import EndingNoteStore
                from deadman.ending_note.guide import EndingNoteGuide
                from deadman.ending_note.models import EndingNote as EN

                # 强制走 auth
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                user_id = user["user_id"]

                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return

                section = req.get("section")
                answer = req.get("answer")
                if not section:
                    self._send_json(400, {
                        "error": "缺少 section",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                if not isinstance(answer, dict):
                    self._send_json(400, {
                        "error": "answer 必须是 dict",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return

                store = EndingNoteStore()
                guide = EndingNoteGuide(store=store)
                note = store.load(user_id) or EN.new(user_id)
                try:
                    note = guide.save_answer(note, section, answer)
                except ValueError as exc:
                    self._send_json(400, {
                        "error": str(exc),
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                store.save(note)

                # 安全信号检测：命中 high 时在响应里附 safety_protocol 提示
                safety = note.safety_flags or {}
                payload = {
                    "ok": True,
                    "note": note.to_dict(),
                    "disclaimer": self._ending_note_disclaimer(),
                }
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
                self._send_json(200, payload)

            def _handle_ending_note_guide_next(self, query: dict[str, list[str]]) -> None:
                """GET /api/ending-note/guide/next?user_id=xxx - 获取下一章引导问题"""
                from deadman.ending_note.store import EndingNoteStore
                from deadman.ending_note.guide import EndingNoteGuide
                from deadman.ending_note.models import EndingNote as EN

                user_id = self._ending_note_user_id(query)
                if not user_id:
                    self._send_json(400, {
                        "error": "缺少 user_id",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                store = EndingNoteStore()
                note = store.load(user_id) or EN.new(user_id)
                guide = EndingNoteGuide(store=store)
                section, title, question = guide.next_question(note)
                self._send_json(200, {
                    "section": section,
                    "title": title,
                    "question": question,
                    "disclaimer": self._ending_note_disclaimer(),
                })

            def _handle_ending_note_share(self) -> None:
                """POST /api/ending-note/share - 共享给家庭成员

                body: {
                    target_user_id: str,
                    sections: list[str] | None  # None = 全部章节
                }
                user_id 从 Authorization 头解析（Phase 14 P0-gap-2 修复）
                """
                from deadman.ending_note.store import EndingNoteStore

                # 强制走 auth
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                user_id = user["user_id"]

                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return

                target_user_id = req.get("target_user_id")
                sections = req.get("sections")
                if not target_user_id:
                    self._send_json(400, {
                        "error": "缺少 target_user_id",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                if sections is not None and not isinstance(sections, list):
                    self._send_json(400, {
                        "error": "sections 必须是 list[str] 或 null",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return

                store = EndingNoteStore()
                try:
                    store.share_with(user_id, target_user_id, sections)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {
                    "ok": True,
                    "shared_with": target_user_id,
                    "sections": sections,
                    "disclaimer": self._ending_note_disclaimer(),
                })

            def _handle_ending_note_unshare(self, query: dict[str, list[str]]) -> None:
                """DELETE /api/ending-note/share?user_id=xxx&target_user_id=xxx"""
                from deadman.ending_note.store import EndingNoteStore
                user_id = self._ending_note_user_id(query)
                target_user_id = query.get("target_user_id", [None])[0]
                if not user_id or not target_user_id:
                    self._send_json(400, {
                        "error": "缺少 user_id 或 target_user_id",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                store = EndingNoteStore()
                store.unshare(user_id, target_user_id)
                self._send_json(200, {
                    "ok": True,
                    "unshared_with": target_user_id,
                    "disclaimer": self._ending_note_disclaimer(),
                })

            def _handle_ending_note_shared_with_me(self, query: dict[str, list[str]]) -> None:
                """GET /api/ending-note/shared-with-me?user_id=xxx - 共享给我的笔记"""
                from deadman.ending_note.store import EndingNoteStore
                user_id = self._ending_note_user_id(query)
                if not user_id:
                    self._send_json(400, {
                        "error": "缺少 user_id",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                store = EndingNoteStore()
                notes = store.list_shared_with_me(user_id)
                self._send_json(200, {
                    "notes": [n.to_dict() for n in notes],
                    "count": len(notes),
                    "disclaimer": self._ending_note_disclaimer(),
                })

            def _handle_ending_note_trigger(self) -> None:
                """POST /api/ending-note/trigger - 触发投递

                body: {
                    trigger_type: "death_confirmation"|"date"|"manual"
                }
                user_id 从 Authorization 头解析（Phase 14 P0-gap-2 修复）
                """
                from deadman.ending_note.store import EndingNoteStore

                # 强制走 auth
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                user_id = user["user_id"]

                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return

                trigger_type = req.get("trigger_type")
                if not trigger_type:
                    self._send_json(400, {
                        "error": "缺少 trigger_type",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return

                store = EndingNoteStore()
                result = store.trigger_delivery(user_id, trigger_type)
                payload = dict(result)
                payload["disclaimer"] = self._ending_note_disclaimer()
                # 死亡确认等待期特别提示
                if trigger_type == "death_confirmation" and not result.get("delivered"):
                    payload["safety_notice"] = (
                        "死亡确认触发已记录。"
                        "等待 7 天是为了避免在情绪冲动下做出不可逆的投递决定。"
                        "等待期内你可以随时取消。"
                    )
                self._send_json(200, payload)

            def _handle_ending_note_completion(self, query: dict[str, list[str]]) -> None:
                """GET /api/ending-note/completion?user_id=xxx - 填写完整度"""
                from deadman.ending_note.store import EndingNoteStore
                from deadman.ending_note.guide import EndingNoteGuide
                from deadman.ending_note.models import EndingNote as EN

                user_id = self._ending_note_user_id(query)
                if not user_id:
                    self._send_json(400, {
                        "error": "缺少 user_id",
                        "disclaimer": self._ending_note_disclaimer(),
                    })
                    return
                store = EndingNoteStore()
                note = store.load(user_id) or EN.new(user_id)
                guide = EndingNoteGuide(store=store)
                rate = guide.completion_rate(note)
                self._send_json(200, {
                    "completion": rate,
                    "disclaimer": self._ending_note_disclaimer(),
                })

            # ==============================================================
            # Phase 11/12/13: 保险库 / 文档提取 / 遗码通 Handler 方法（只追加）
            # ==============================================================
            def _phase_auth_user(self) -> dict | None:
                """通用：从 Authorization 头解析当前用户，未认证返回 None"""
                headers = self._get_headers_dict()
                return server_ref._require_auth(headers)

            def _phase_unauthorized(self) -> None:
                self._send_json(401, {"error": "未认证或 token 无效"})

            # ---------- Phase 11: Vault ----------
            def _handle_vault_items_list(self, query: dict[str, list[str]]) -> None:
                """GET /api/vault/items - 列出我的条目"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.vault.store import VaultStore
                store = VaultStore()
                items = store.list_items(user["user_id"], user["user_id"])
                self._send_json(200, {"items": items})

            def _handle_vault_item_get(self, item_id: str) -> None:
                """GET /api/vault/items/<id> - 获取条目详情"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.vault.store import VaultStore
                store = VaultStore()
                item = store.get_item(item_id, user["user_id"])
                if item is None:
                    self._send_json(404, {"error": "条目不存在或无权限"})
                    return
                # 不返回 content_encrypted（二进制）；只返回元数据
                resp = item.to_index_dict()
                self._send_json(200, resp)

            def _handle_vault_item_add(self) -> None:
                """POST /api/vault/items - 添加条目

                body: {type, title, content, beneficiary_user_ids,
                       delivery_trigger?, delivery_date?, metadata?}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return
                try:
                    from deadman.vault.store import VaultStore
                    store = VaultStore()
                    content = req.get("content", "")
                    # content 可以是 str 或 base64
                    if isinstance(content, str) and content.startswith("base64:"):
                        import base64
                        content = base64.b64decode(content[len("base64:"):])
                    delivery_date_str = req.get("delivery_date")
                    delivery_date = None
                    if delivery_date_str:
                        try:
                            from datetime import datetime as _dt
                            delivery_date = _dt.fromisoformat(delivery_date_str)
                        except (TypeError, ValueError):
                            delivery_date = None
                    item = store.add_item(
                        owner_user_id=user["user_id"],
                        type=req.get("type", "note"),
                        title=req.get("title", ""),
                        content=content,
                        beneficiary_user_ids=req.get("beneficiary_user_ids", []) or [],
                        delivery_trigger=req.get("delivery_trigger", "manual"),
                        delivery_date=delivery_date,
                        metadata=req.get("metadata") or {},
                    )
                    self._send_json(201, item.to_index_dict())
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                except Exception as exc:
                    logger.exception("vault item add failed")
                    self._send_json(500, {"error": f"server error: {exc}"})

            def _handle_vault_item_delete(self, item_id: str) -> None:
                """DELETE /api/vault/items/<id> - 删除条目（仅 owner）"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.vault.store import VaultStore
                store = VaultStore()
                ok = store.delete_item(item_id, user["user_id"])
                if ok:
                    self._send_json(200, {"deleted": True})
                else:
                    self._send_json(404, {"error": "条目不存在或无权限"})

            def _handle_vault_beneficiaries(self) -> None:
                """GET /api/vault/beneficiaries - 列出我指定的受益人"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.vault.store import VaultStore
                store = VaultStore()
                beneficiaries = store.list_beneficiaries(user["user_id"])
                self._send_json(200, {"beneficiaries": beneficiaries})

            def _handle_vault_inherited(self) -> None:
                """GET /api/vault/inherited - 列出我能继承的"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.vault.store import VaultStore
                store = VaultStore()
                inherited = store.list_inherited(user["user_id"])
                self._send_json(200, {"inherited": inherited})

            def _handle_vault_item_trigger(self, item_id: str) -> None:
                """POST /api/vault/items/<id>/trigger - 触发投递

                body: {trigger_type: on_death | on_date | manual}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    req = {}
                trigger_type = req.get("trigger_type", "manual")
                from deadman.vault.store import VaultStore
                store = VaultStore()
                result = store.trigger_delivery(item_id, trigger_type, user["user_id"])
                # content 是 bytes，转 base64
                if result.get("content") is not None:
                    import base64
                    result["content_b64"] = base64.b64encode(result["content"]).decode("ascii")
                    result["content"] = None  # 不直接放 bytes 进 JSON
                self._send_json(200, result)

            # ---------- Phase 12: Document Extract ----------
            def _handle_documents_list(self) -> None:
                """GET /api/documents - 列出我的文档"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.doc_extract.extractor import DocumentExtractor
                extractor = DocumentExtractor()
                docs = extractor.list_my_documents(user["user_id"])
                self._send_json(200, {"documents": [d.to_dict() for d in docs]})

            def _handle_document_get(self, doc_id: str) -> None:
                """GET /api/documents/<id> - 文档详情"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.doc_extract.extractor import DocumentExtractor
                extractor = DocumentExtractor()
                doc = extractor.get_document(doc_id, user["user_id"])
                if doc is None:
                    self._send_json(404, {"error": "文档不存在或无权限"})
                    return
                self._send_json(200, doc.to_dict())

            def _handle_document_extract(self) -> None:
                """POST /api/documents/extract - 上传文档并提取

                支持 multipart/form-data（field: file, doc_type?）
                或 JSON {filename, content_base64, doc_type?}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                ct = self.headers.get("Content-Type", "")
                filename = ""
                content: bytes = b""
                doc_type_hint = None
                if ct.startswith("multipart/form-data"):
                    parsed = server_ref._parse_multipart(self)
                    filename = parsed.get("filename", "")
                    content = parsed.get("content", b"")
                    doc_type_hint = parsed.get("doc_type") or None
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        req = json.loads(raw.decode("utf-8")) if raw else {}
                    except json.JSONDecodeError as exc:
                        self._send_json(400, {"error": f"invalid json: {exc}"})
                        return
                    filename = req.get("filename", "")
                    import base64
                    try:
                        content = base64.b64decode(req.get("content_base64", ""))
                    except Exception:
                        content = b""
                    doc_type_hint = req.get("doc_type")
                if not filename or not content:
                    self._send_json(400, {"error": "缺少 filename 或 content"})
                    return
                try:
                    from deadman.doc_extract.extractor import DocumentExtractor
                    extractor = DocumentExtractor()
                    doc = asyncio.run(
                        extractor.extract(
                            owner_user_id=user["user_id"],
                            filename=filename,
                            content=content,
                            doc_type_hint=doc_type_hint,
                        )
                    )
                    self._send_json(201, doc.to_dict())
                except Exception as exc:
                    logger.exception("document extract failed")
                    self._send_json(500, {"error": f"server error: {exc}"})

            def _handle_document_delete(self, doc_id: str) -> None:
                """DELETE /api/documents/<id> - 删除文档"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.doc_extract.extractor import DocumentExtractor
                extractor = DocumentExtractor()
                ok = extractor.delete_document(doc_id, user["user_id"])
                if ok:
                    self._send_json(200, {"deleted": True})
                else:
                    self._send_json(404, {"error": "文档不存在或无权限"})

            # ---------- Phase 13: Decedent ID ----------
            def _handle_cases_list(self) -> None:
                """GET /api/cases - 列出我的案例"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.decedent_id.registry import DecedentRegistry
                reg = DecedentRegistry()
                cases = reg.list_cases(user["user_id"])
                self._send_json(200, {"cases": [c.to_dict() for c in cases]})

            def _handle_case_get(self, case_id: str) -> None:
                """GET /api/cases/<id> - 案例详情"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.decedent_id.registry import DecedentRegistry
                reg = DecedentRegistry()
                case = reg.get_case(case_id, user["user_id"])
                if case is None:
                    self._send_json(404, {"error": "案例不存在或无权限"})
                    return
                self._send_json(200, case.to_dict())

            def _handle_case_create(self) -> None:
                """POST /api/cases - 创建案例

                body: {decedent_alias, relationship}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return
                try:
                    from deadman.decedent_id.registry import DecedentRegistry
                    reg = DecedentRegistry()
                    case = reg.create_case(
                        owner_user_id=user["user_id"],
                        decedent_alias=req.get("decedent_alias", ""),
                        relationship=req.get("relationship", "其他"),
                    )
                    self._send_json(201, case.to_dict())
                except Exception as exc:
                    logger.exception("case create failed")
                    self._send_json(500, {"error": f"server error: {exc}"})

            def _handle_case_event_add(self, case_id: str) -> None:
                """POST /api/cases/<id>/events - 添加事件

                body: {event, agent, notes?}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return
                from deadman.decedent_id.registry import DecedentRegistry
                reg = DecedentRegistry()
                case = reg.add_event(
                    case_id=case_id,
                    owner_user_id=user["user_id"],
                    event=req.get("event", ""),
                    agent=req.get("agent", "unknown"),
                    notes=req.get("notes", "") or "",
                )
                if case is None:
                    self._send_json(404, {"error": "案例不存在或无权限"})
                    return
                self._send_json(200, case.to_dict())

            def _handle_case_archive(self, case_id: str) -> None:
                """POST /api/cases/<id>/archive - 归档案例"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.decedent_id.registry import DecedentRegistry
                reg = DecedentRegistry()
                ok = reg.archive_case(case_id, user["user_id"])
                if ok:
                    self._send_json(200, {"archived": True})
                else:
                    self._send_json(404, {"error": "案例不存在或无权限"})

            def _handle_case_timeline(self, case_id: str) -> None:
                """GET /api/cases/<id>/timeline - 时间线"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.decedent_id.registry import DecedentRegistry
                reg = DecedentRegistry()
                timeline = reg.get_timeline(case_id, user["user_id"])
                self._send_json(200, {"timeline": timeline})

            # ==============================================================
            # Phase 15: Dead Man Switch Handler 方法（只追加）
            # ==============================================================
            def _switch_read_body(self) -> dict | None:
                """读取 POST JSON body，返回 dict；JSON 解析失败返回 None（已写 400）"""
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return None

            def _handle_switch_init(self) -> None:
                """POST /api/switch/init - 初始化配置

                body: {
                    frequency?, missed?, window?, cooldown?,
                    emergency_contacts: [user_id], lawyer_id?, heir_ids: [user_id],
                    email?, phone?
                }
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                req = self._switch_read_body()
                if req is None:
                    return
                from deadman.deadman_switch.models import SwitchConfig
                from deadman.deadman_switch.store import SwitchStore
                config = SwitchConfig(
                    check_in_frequency_days=int(req.get("frequency", 30)),
                    missed_threshold=int(req.get("missed", 3)),
                    verification_window_days=int(req.get("window", 7)),
                    cooldown_days=max(int(req.get("cooldown", 7)), 7),
                    emergency_contacts=list(req.get("emergency_contacts", []) or []),
                    lawyer_user_id=req.get("lawyer_id"),
                    heir_user_ids=list(req.get("heir_ids", []) or []),
                )
                if req.get("email"):
                    config.set_email(str(req["email"]))
                if req.get("phone"):
                    config.set_phone(str(req["phone"]))
                store = SwitchStore()
                record = store.init_switch(user["user_id"], config)
                self._send_json(201, record.to_dict())

            def _handle_switch_checkin(self) -> None:
                """POST /api/switch/checkin - 用户 check-in"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                req = self._switch_read_body()
                if req is None:
                    return
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                method = req.get("method", "web")
                record = store.record_check_in(user["user_id"], method=method)
                if record is None:
                    self._send_json(404, {"error": "switch not initialized"})
                    return
                self._send_json(200, record.to_dict())

            def _handle_switch_status(self) -> None:
                """GET /api/switch/status - 查看状态"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                record = store.load(user["user_id"])
                if record is None:
                    self._send_json(404, {"error": "switch not initialized"})
                    return
                payload = record.to_dict()
                # 附带冷静期剩余天数（CONFIRMED 状态）
                if record.state.value == "CONFIRMED":
                    payload["cooldown_remaining_days"] = store.cooldown_remaining_days(
                        user["user_id"]
                    )
                    payload["cooldown_passed"] = store.is_cooldown_passed(user["user_id"])
                self._send_json(200, payload)

            def _handle_switch_tick(self) -> None:
                """POST /api/switch/tick - 手动触发状态机检查（Cron 调用）"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                record = store.tick(user["user_id"])
                if record is None:
                    self._send_json(404, {"error": "switch not initialized"})
                    return
                self._send_json(200, {"state": record.state.value, "record": record.to_dict()})

            def _handle_switch_verify_contact(self) -> None:
                """POST /api/switch/verify-contact

                body: {contact_id, confirm: bool}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                req = self._switch_read_body()
                if req is None:
                    return
                contact_id = req.get("contact_id")
                confirm = bool(req.get("confirm", False))
                if not contact_id:
                    self._send_json(400, {"error": "缺少 contact_id"})
                    return
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                record, msg = store.verify_emergency_contact(
                    user["user_id"], str(contact_id), confirm
                )
                if record is None:
                    self._send_json(404, {"error": msg})
                    return
                self._send_json(200, {"message": msg, "record": record.to_dict()})

            def _handle_switch_verify_heir(self) -> None:
                """POST /api/switch/verify-heir

                body: {heir_id, confirm: bool}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                req = self._switch_read_body()
                if req is None:
                    return
                heir_id = req.get("heir_id")
                confirm = bool(req.get("confirm", False))
                if not heir_id:
                    self._send_json(400, {"error": "缺少 heir_id"})
                    return
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                record, msg = store.verify_heir(
                    user["user_id"], str(heir_id), confirm
                )
                if record is None:
                    self._send_json(404, {"error": msg})
                    return
                self._send_json(200, {"message": msg, "record": record.to_dict()})

            def _handle_switch_cancel(self) -> None:
                """POST /api/switch/cancel

                body: {reason?}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                req = self._switch_read_body()
                if req is None:
                    return
                reason = str(req.get("reason", "user_cancelled"))
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                record = store.cancel(user["user_id"], reason=reason)
                if record is None:
                    self._send_json(404, {"error": "switch not initialized"})
                    return
                self._send_json(200, record.to_dict())

            def _handle_switch_list_actions(self) -> None:
                """GET /api/switch/actions - 列出待执行动作"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                record = store.load(user["user_id"])
                if record is None:
                    self._send_json(404, {"error": "switch not initialized"})
                    return
                self._send_json(200, {
                    "pending_actions": record.pending_actions,
                    "executed_actions": record.executed_actions,
                    "state": record.state.value,
                })

            def _handle_switch_execute(self) -> None:
                """POST /api/switch/execute - 执行 CONFIRMED → EXECUTED

                safety-protocol.md：必须先过冷静期（cooldown_days）
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.deadman_switch.actions import SwitchActionExecutor
                from deadman.deadman_switch.store import SwitchStore
                store = SwitchStore()
                executor = SwitchActionExecutor(store=store)
                try:
                    result = executor.execute_confirmed(user["user_id"])
                except RuntimeError as exc:
                    self._send_json(409, {"error": str(exc)})
                    return
                self._send_json(200, result)

            # ==============================================================
            # Phase 15: 通知信函生成器 Handler 方法（只追加）
            # ==============================================================
            def _handle_letters_types(self) -> None:
                """GET /api/letters/types - 列出 8 种信函类型

                需认证（_phase_auth_user）；返回 types 列表 + disclaimer
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.notification_letters.templates import LETTER_TYPES
                from deadman.notification_letters.models import DEFAULT_DISCLAIMER
                self._send_json(200, {
                    "types": [dict(t) for t in LETTER_TYPES],
                    "count": len(LETTER_TYPES),
                    "disclaimer": DEFAULT_DISCLAIMER,
                })

            def _handle_letters_template(self, query: dict[str, list[str]]) -> None:
                """GET /api/letters/template?type=xxx - 返回原始模板

                需认证；返回原始模板文本（不填充）
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.notification_letters.templates import (
                    LETTER_TEMPLATES,
                    LETTER_TYPES,
                    get_letter_type_meta,
                )
                from deadman.notification_letters.models import DEFAULT_DISCLAIMER
                letter_type = (query.get("type", [""])[0] or "").strip()
                if not letter_type:
                    self._send_json(400, {
                        "error": "缺少 type 参数",
                        "disclaimer": DEFAULT_DISCLAIMER,
                    })
                    return
                if letter_type not in LETTER_TEMPLATES:
                    self._send_json(404, {
                        "error": f"未知信函类型: {letter_type}",
                        "supported_types": [t["type"] for t in LETTER_TYPES],
                        "disclaimer": DEFAULT_DISCLAIMER,
                    })
                    return
                meta = get_letter_type_meta(letter_type) or {}
                self._send_json(200, {
                    "type": letter_type,
                    "name": meta.get("name", ""),
                    "template": LETTER_TEMPLATES[letter_type],
                    "extra_fields_needed": meta.get("extra_fields_needed", []),
                    "disclaimer": DEFAULT_DISCLAIMER,
                })

            def _handle_letters_generate(self) -> None:
                """POST /api/letters/generate - 生成通知信函

                body: LetterRequest 字段（letter_type / decedent_name /
                      decedent_id_masked / death_date / applicant_name /
                      applicant_relationship / recipient_org /
                      extra_fields / language / use_llm）

                返回 LetterResult（text / letter_type / confidence /
                        placeholders / disclaimer）
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return

                from deadman.notification_letters import (
                    LetterGenerator,
                    LetterRequest,
                )
                from deadman.notification_letters.models import DEFAULT_DISCLAIMER

                letter_type = req.get("letter_type")
                if not letter_type:
                    self._send_json(400, {
                        "error": "缺少 letter_type",
                        "disclaimer": DEFAULT_DISCLAIMER,
                    })
                    return

                try:
                    request = LetterRequest(
                        letter_type=letter_type,
                        decedent_name=req.get("decedent_name", "") or "",
                        decedent_id_masked=req.get("decedent_id_masked", "") or "",
                        death_date=req.get("death_date", "") or "",
                        applicant_name=req.get("applicant_name", "") or "",
                        applicant_relationship=req.get("applicant_relationship", "") or "",
                        recipient_org=req.get("recipient_org", "") or "",
                        extra_fields=req.get("extra_fields") or {},
                        language=req.get("language", "zh-CN") or "zh-CN",
                    )
                except (TypeError, ValueError) as exc:
                    self._send_json(400, {
                        "error": f"请求参数无效: {exc}",
                        "disclaimer": DEFAULT_DISCLAIMER,
                    })
                    return

                use_llm = bool(req.get("use_llm", False))
                generator = LetterGenerator(use_llm=use_llm)
                try:
                    result = generator.generate(request)
                except ValueError as exc:
                    self._send_json(400, {
                        "error": str(exc),
                        "disclaimer": DEFAULT_DISCLAIMER,
                    })
                    return
                self._send_json(200, result.to_dict())

            # ==============================================================
            # Phase 15: plan_score 规划完整度评分 Handler 方法（只追加）
            # ==============================================================
            _PLAN_SCORE_DISCLAIMER = (
                "评分仅反映信息完整度，不代表法律效力；"
                "建议结合律师/公证处专业意见。"
            )

            def _handle_plan_score(self) -> None:
                """GET /api/plan-score - 获取当前用户的规划完整度评分

                响应：{user_id, total_score, category_scores, overall_suggestions,
                       generated_at, disclaimer}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.plan_score.scorer import PlanScorer
                scorer = PlanScorer()
                result = scorer.score(user["user_id"])
                payload = result.to_dict()
                payload["disclaimer"] = self._PLAN_SCORE_DISCLAIMER
                self._send_json(200, payload)

            def _handle_plan_score_detail(self) -> None:
                """GET /api/plan-score/detail - 获取详细分解

                响应：同 /api/plan-score，但 category_scores 内的每条 SubScore
                含完整 completed_items / missing_items / suggestions 列表
                （/api/plan-score 也是完整的；detail 端点为前端语义清晰预留）
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.plan_score.scorer import PlanScorer
                scorer = PlanScorer()
                result = scorer.score(user["user_id"])
                payload = result.to_dict()
                payload["disclaimer"] = self._PLAN_SCORE_DISCLAIMER
                self._send_json(200, payload)

            # ---------- Phase 15 (Memorial Writer): AI 悼文撰写 ----------
            _MEMORIAL_DISCLAIMER = (
                "AI 生成的悼文仅供参考，建议家属审阅修改后使用。"
            )

            def _handle_memorial_types(self) -> None:
                """GET /api/memorial/types - 列出 5 种悼文文档类型

                响应：{types: [{key, name, name_en, description, word_range}]}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.memorial_writer.models import (
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
                self._send_json(200, {
                    "types": types_list,
                    "tones": list(VALID_TONES),
                    "faiths": list(VALID_FAITHS),
                    "languages": list(VALID_LANGUAGES),
                    "disclaimer": self._MEMORIAL_DISCLAIMER,
                })

            def _handle_memorial_generate(self) -> None:
                """POST /api/memorial/generate - 生成悼文/讣告/答谢词/墓志铭/追思会致辞

                body: MemorialRequest 字段
                    doc_type, decedent_name, relationship?, personality_traits?,
                    memories?, values_or_sayings?, tone?, faith?, language?, word_limit?

                响应：{text, doc_type, confidence, safety_flags, alternatives, disclaimer}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {
                        "error": f"invalid json: {exc}",
                        "disclaimer": self._MEMORIAL_DISCLAIMER,
                    })
                    return
                from deadman.memorial_writer.models import MemorialRequest
                from deadman.memorial_writer.generator import MemorialGenerator
                try:
                    request = MemorialRequest.from_dict(req)
                except (TypeError, ValueError) as exc:
                    self._send_json(400, {
                        "error": f"参数解析失败: {exc}",
                        "disclaimer": self._MEMORIAL_DISCLAIMER,
                    })
                    return
                errors = request.validate()
                if errors:
                    self._send_json(400, {
                        "error": "参数校验失败: " + "; ".join(errors),
                        "disclaimer": self._MEMORIAL_DISCLAIMER,
                    })
                    return
                try:
                    gen = MemorialGenerator()
                    result = asyncio.run(gen.generate(request))
                except ValueError as exc:
                    self._send_json(400, {
                        "error": str(exc),
                        "disclaimer": self._MEMORIAL_DISCLAIMER,
                    })
                    return
                except Exception as exc:
                    logger.exception("memorial generate failed")
                    self._send_json(500, {
                        "error": f"server error: {exc}",
                        "disclaimer": self._MEMORIAL_DISCLAIMER,
                    })
                    return
                payload = result.to_dict()
                payload["disclaimer"] = self._MEMORIAL_DISCLAIMER
                self._send_json(200, payload)

            # ==============================================================
            # Phase 16C: 合规页面 + 客服工单 + Onboarding Handler 方法（只追加）
            # ==============================================================
            _DOCS_DISCLAIMER = (
                "本页面内容由 deadman 平台整理，不替代法律/医疗/财务专业意见。"
                "具体条款以最新版本为准。"
            )

            def _handle_docs_page(self, name: str) -> None:
                """GET /privacy | /terms | /support - 返回 docs/<name>.md 渲染为 HTML

                不引入 markdown 库；用 <pre> 简单包装即可（约束：不引入新依赖）。
                需 HTML escape 避免注入。
                """
                docs_dir = settings.project_root.parent / "docs"
                md_path = docs_dir / f"{name}.md"
                if not md_path.exists():
                    self._send_json(404, {
                        "error": f"未找到文档: {name}",
                        "disclaimer": self._DOCS_DISCLAIMER,
                    })
                    return
                try:
                    raw = md_path.read_text(encoding="utf-8")
                except OSError as exc:
                    self._send_json(500, {"error": f"读取失败: {exc}"})
                    return
                # HTML escape 防注入
                escaped = (
                    raw.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
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
                    "<div class='doc-footer'>" + self._DOCS_DISCLAIMER + "</div>"
                    "</div></body></html>"
                )
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            # ---------- Phase 16C: 客服工单 ----------

            _SUPPORT_DISCLAIMER = (
                "工单系统用于反馈/咨询/投诉，不替代紧急救援；"
                "如有自伤/自杀风险请立即拨打 120 / 110 / 400-161-9995。"
            )

            def _handle_support_tickets_list(self) -> None:
                """GET /api/support/tickets - 列出我的工单"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.support.store import TicketStore
                store = TicketStore()
                tickets = store.list_user_tickets(user["user_id"])
                self._send_json(200, {
                    "tickets": [t.to_dict() for t in tickets],
                    "count": len(tickets),
                    "disclaimer": self._SUPPORT_DISCLAIMER,
                })

            def _handle_support_ticket_get(self, ticket_id: str) -> None:
                """GET /api/support/tickets/<id> - 工单详情（含 ownership 校验）"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.support.store import TicketStore
                store = TicketStore()
                ticket = store.get_ticket(ticket_id, user["user_id"])
                if ticket is None:
                    self._send_json(404, {
                        "error": "工单不存在或无权限",
                        "ticket_id": ticket_id,
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                self._send_json(200, {
                    "ticket": ticket.to_dict(),
                    "disclaimer": self._SUPPORT_DISCLAIMER,
                })

            def _handle_support_ticket_create(self) -> None:
                """POST /api/support/tickets - 创建工单

                body: {category, priority, subject, description}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {
                        "error": f"invalid json: {exc}",
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                try:
                    from deadman.support.store import TicketStore
                    store = TicketStore()
                    ticket = store.create_ticket(
                        user_id=user["user_id"],
                        category=req.get("category", "咨询"),
                        priority=req.get("priority", "普通"),
                        subject=req.get("subject", ""),
                        description=req.get("description", ""),
                    )
                except ValueError as exc:
                    self._send_json(400, {
                        "error": str(exc),
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                except Exception as exc:
                    logger.exception("support ticket create failed")
                    self._send_json(500, {
                        "error": f"server error: {exc}",
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                self._send_json(201, {
                    "ticket": ticket.to_dict(),
                    "disclaimer": self._SUPPORT_DISCLAIMER,
                })

            def _handle_support_ticket_reply(self, ticket_id: str) -> None:
                """POST /api/support/tickets/<id>/replies - 追加回复（仅 user 角色）

                body: {content}
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {
                        "error": f"invalid json: {exc}",
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                content = req.get("content", "")
                if not content or not str(content).strip():
                    self._send_json(400, {
                        "error": "缺少 content",
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                from deadman.support.store import TicketStore
                store = TicketStore()
                reply = store.add_reply(
                    ticket_id=ticket_id,
                    author="user",
                    content=str(content),
                    user_id=user["user_id"],
                )
                if reply is None:
                    self._send_json(404, {
                        "error": "工单不存在或无权限",
                        "ticket_id": ticket_id,
                        "disclaimer": self._SUPPORT_DISCLAIMER,
                    })
                    return
                self._send_json(200, {
                    "reply": reply.to_dict(),
                    "disclaimer": self._SUPPORT_DISCLAIMER,
                })

            # ---------- Phase 16C: Onboarding ----------

            _ONBOARDING_DISCLAIMER = (
                "Onboarding 画像用于个性化引导，可随时通过重新引导修改；"
                "如不再使用本平台，可在帮助中心申请数据删除。"
            )

            def _handle_onboarding_get(self) -> None:
                """GET /api/onboarding - 返回当前用户 onboarding 画像（无则 null）"""
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                from deadman.onboarding.store import OnboardingStore
                store = OnboardingStore()
                profile = store.load(user["user_id"])
                if profile is None:
                    self._send_json(200, {
                        "profile": None,
                        "completed": False,
                        "disclaimer": self._ONBOARDING_DISCLAIMER,
                    })
                    return
                self._send_json(200, {
                    "profile": profile.to_dict(),
                    "completed": True,
                    "disclaimer": self._ONBOARDING_DISCLAIMER,
                })

            def _handle_onboarding_step(self, step_str: str) -> None:
                """GET /api/onboarding/step/<index> - 返回第 N 步问题

                未认证也允许查看步骤定义（不暴露 PII）。
                """
                from deadman.onboarding.wizard import OnboardingWizard
                try:
                    idx = int(step_str)
                except (TypeError, ValueError):
                    self._send_json(400, {"error": f"step 必须是整数，收到: {step_str}"})
                    return
                wiz = OnboardingWizard()
                try:
                    step = wiz.get_step(idx)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {
                    "step": step,
                    "total_steps": wiz.TOTAL_STEPS,
                    "disclaimer": self._ONBOARDING_DISCLAIMER,
                })

            def _handle_onboarding_save(self) -> None:
                """POST /api/onboarding - 保存 onboarding 画像

                body: {
                    relationship: "亲属"|"朋友"|"本人"|"其他",
                    location: str,
                    death_date?: str (YYYY-MM-DD),
                    current_stage?: list[str],
                    consent: bool
                }
                """
                user = self._phase_auth_user()
                if user is None:
                    self._phase_unauthorized()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as exc:
                    self._send_json(400, {
                        "error": f"invalid json: {exc}",
                        "disclaimer": self._ONBOARDING_DISCLAIMER,
                    })
                    return
                from deadman.onboarding.store import OnboardingStore
                from deadman.onboarding.wizard import OnboardingWizard
                store = OnboardingStore()
                wiz = OnboardingWizard(store=store)
                try:
                    profile = wiz.save_profile(user["user_id"], req)
                except ValueError as exc:
                    self._send_json(400, {
                        "error": str(exc),
                        "disclaimer": self._ONBOARDING_DISCLAIMER,
                    })
                    return
                except Exception as exc:
                    logger.exception("onboarding save failed")
                    self._send_json(500, {
                        "error": f"server error: {exc}",
                        "disclaimer": self._ONBOARDING_DISCLAIMER,
                    })
                    return
                self._send_json(200, {
                    "profile": profile.to_dict(),
                    "user_profile": OnboardingWizard.to_user_profile(profile),
                    "completed": True,
                    "disclaimer": self._ONBOARDING_DISCLAIMER,
                })

        httpd = ThreadingHTTPServer((host, port), Handler)
        logger.info("AG-UI Web Server listening on http://%s:%d", host, port)
        print(f"AG-UI Web Server listening on http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()

    async def _handle_chat(
        self,
        agent: str,
        query: str,
        history: list,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """处理对话 - 走 orchestration/graph 完整规则链

        关键变更：不再直接调 llm_client.chat()，而是：
        1. 构造 ConversationState（含 user_input / current_agent / user_id / history）
        2. 调 build_main_graph().ainvoke(state)
        3. 从 state 提取 response / agent / risk_tier / safety_triggered
        4. 调 MemoryManager.after_turn 更新记忆

        graph 失败时降级到 llm_client，但用 SoulLoader.default_soul()
        作为最低身份约束（不再用硬编码 system prompt）。
        """
        if not query:
            return {"error": "query 不能为空"}

        from ..orchestration.graph import build_main_graph
        from ..orchestration.state import ConversationState
        from ..memory.manager import MemoryManager

        # 构造 state
        session_id = f"web-{user_id or 'anon'}-{int(time.time())}"
        # 智能体名归一化：前端用短横线，graph 内部 AGENT_NAMES 用下划线
        agent_normalized = (agent or "death-aftercare").replace("-", "_")
        state = ConversationState(
            user_input=query,
            current_agent=agent_normalized,
            session_id=session_id,
            # 以下字段不在 ConversationState TypedDict 定义中，
            # 但 total=False 运行时允许透传（节点用 .get() 读取，未定义字段被忽略）
            agent_name=agent_normalized,  # type: ignore[typeddict-unknown-key]
            user_id=user_id or "anonymous",  # type: ignore[typeddict-unknown-key]
            history=list(history[-10:]),  # type: ignore[typeddict-unknown-key]
        )

        # 走 graph（含 input_guard / router / agent_node / rule_check / output_guard / respond）
        try:
            graph = build_main_graph()
            result_state = await graph.ainvoke(state)

            # 提取响应
            response = (
                result_state.get("final_response")
                or result_state.get("draft_response", "")
            )
            actual_agent = result_state.get("current_agent") or agent_normalized
            # 转回短横线格式（与前端 agent ID 一致）
            actual_agent = actual_agent.replace("_", "-")

            # risk_tier / safety_triggered / rule_violations 来自 rule_check
            rule_check = result_state.get("rule_check")
            if rule_check is not None:
                risk_tier = getattr(
                    getattr(rule_check, "risk_tier", None), "value", "R0"
                )
                safety_triggered = bool(
                    getattr(rule_check, "safety_triggered", False)
                )
                rule_violations = list(
                    getattr(rule_check, "violations", []) or []
                )
            else:
                risk_tier = "R0"
                safety_triggered = False
                rule_violations = []

            # 更新记忆（best-effort，失败不影响响应）
            try:
                mm = MemoryManager()
                await mm.after_turn(
                    user_id=user_id or "anonymous",
                    user_input=query,
                    assistant_response=response,
                    agent=actual_agent,
                    session_id=session_id,
                    risk_tier=risk_tier,
                )
            except Exception as exc:
                logger.warning(
                    "MemoryManager.after_turn 失败（不影响响应）: %s", exc
                )

            return {
                "response": response,
                "agent": actual_agent,
                "risk_tier": risk_tier,
                "safety_triggered": safety_triggered,
                "rule_violations": rule_violations,
                "degraded": False,
            }
        except Exception as exc:
            logger.exception("graph 调用失败")
            # 降级：仍调 llm_client 但明确标记 degraded，不再用硬编码 system prompt
            # 而是用 SoulLoader.default_soul() 作为最低身份约束
            from ..soul_loader import SoulLoader
            from ..llm import llm_client

            if not llm_client.api_key:
                return {
                    "response": "服务暂不可用（LLM 未配置）。",
                    "agent": agent,
                    "degraded": True,
                    "error": "llm_not_configured",
                }
            messages: list[dict[str, str]] = [
                {"role": "system", "content": SoulLoader().default_soul()},
            ] + [
                {"role": item.get("role", "user"), "content": item.get("content", "")}
                for item in history[-10:]
                if item.get("role") in ("user", "assistant") and item.get("content")
            ] + [{"role": "user", "content": query}]
            try:
                response = await llm_client.chat(messages, temperature=0.3)
                return {
                    "response": response,
                    "agent": agent,
                    "degraded": True,
                    "degraded_reason": "graph_failed_using_fallback",
                    "error": str(exc),
                }
            except Exception as fallback_exc:
                return {
                    "response": f"服务暂不可用: {fallback_exc}",
                    "agent": agent,
                    "degraded": True,
                    "error": str(fallback_exc),
                }

    def _handle_whoami(self) -> dict[str, Any]:
        """GET/POST /api/whoami - 平台身份告知（transparency-framework L5 强制）

        返回平台基本信息，明确告知是 AI（transparency-framework 要求），
        附带服务边界免责声明（service-boundary-framework 四项禁止）。
        """
        return {
            "platform": "deadman",
            "version": "5.0.0",
            "is_ai": True,  # transparency-framework L5 强制
            "disclaimer": (
                "本平台是信息引导工具，不代办、不代查、不出具法律意见、"
                "不与殡葬机构分成。"
            ),
            "rules_count": 15,
            "agents": [
                "death-aftercare",
                "legal-advisor",
                "financial-analyst",
                "policy-researcher",
                "cross-border-specialist",
                "medical-guide",
            ],
            "supported_languages": ["zh-CN", "en-US"],
        }

    def _handle_cli(self, command: str, req: dict[str, Any]) -> dict[str, Any]:
        """通用 CLI 代理 - subprocess 调用 deadman.cli <command>

        安全：command 必须在 _CLI_COMMANDS 白名单中
        返回：{"ok": bool, "output": str, "command": str, "returncode": int}
        """
        if command not in _CLI_COMMANDS:
            return {
                "ok": False,
                "error": f"不允许的命令: {command}",
                "allowed": sorted(_CLI_COMMANDS),
            }

        # 构造命令行参数
        cmd_args = [sys.executable, "-m", "deadman.cli", command]

        # 从 req 中提取额外参数（如 --provider, --model, --name, --timeout 等）
        extra_args = req.get("args", [])
        if isinstance(extra_args, list):
            cmd_args.extend(str(a) for a in extra_args)

        timeout = req.get("timeout", 60)

        try:
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(settings.project_root),
            )
            return {
                "ok": proc.returncode == 0,
                "output": proc.stdout,
                "stderr": proc.stderr,
                "command": command,
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"命令超时（{timeout}s）",
                "command": command,
                "returncode": -1,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "command": command,
                "returncode": -1,
            }

    async def _stream_chat(
        self,
        wfile: Any,
        query: str,
        agent: str,
        user_id: str | None = None,
    ) -> None:
        """SSE 流式推送对话 - 走完整 graph 规则链（Phase 14 P0 修复）

        关键变更（PM v2 P0-gap-1）：原实现硬编码 system prompt 直接调
        llm_client.chat_stream()，绕过 input_guard/router/agent_node/rule_check/
        output_guard/respond 全部规则节点。现统一走 build_main_graph().ainvoke(state)，
        与 /api/chat 同等约束（safety-protocol L0 / integrity L1 / compliance L3 全部生效）。

        由于 graph 当前不支持 token 级 astream，采用「先 graph 完整跑 → 再 SSE 分块推送」
        的折中方案：用户感知是流式输出（按句号/换行切块），但规则链 100% 生效。
        后续若 graph 支持 astream 事件，可改为真正的 token 级流式。
        """
        if not query:
            wfile.write(
                b"event: error\ndata: "
                + json.dumps({"error": "query 不能为空"}).encode()
                + b"\n\n"
            )
            wfile.flush()
            return

        from ..orchestration.graph import build_main_graph
        from ..orchestration.state import ConversationState
        from ..memory.manager import MemoryManager
        from ..soul_loader import SoulLoader
        from ..llm import llm_client

        # 智能体名归一化
        agent_normalized = (agent or "death-aftercare").replace("-", "_")
        session_id = f"web-stream-{user_id or 'anon'}-{int(time.time())}"
        state = ConversationState(
            user_input=query,
            current_agent=agent_normalized,
            session_id=session_id,
            agent_name=agent_normalized,  # type: ignore[typeddict-unknown-key]
            user_id=user_id or "anonymous",  # type: ignore[typeddict-unknown-key]
            history=[],  # type: ignore[typeddict-unknown-key]
        )

        response_text = ""
        degraded = False
        risk_tier = "R0"
        safety_triggered = False

        # 走 graph（与 _handle_chat 一致的规则链）
        try:
            graph = build_main_graph()
            result_state = await graph.ainvoke(state)
            response_text = (
                result_state.get("final_response")
                or result_state.get("draft_response", "")
            )
            rule_check = result_state.get("rule_check")
            if rule_check is not None:
                risk_tier = getattr(
                    getattr(rule_check, "risk_tier", None), "value", "R0"
                )
                safety_triggered = bool(
                    getattr(rule_check, "safety_triggered", False)
                )
            # 更新记忆
            try:
                mm = MemoryManager()
                await mm.after_turn(
                    user_id=user_id or "anonymous",
                    user_input=query,
                    assistant_response=response_text,
                    agent=agent_normalized.replace("_", "-"),
                    session_id=session_id,
                    risk_tier=risk_tier,
                )
            except Exception as exc:
                logger.warning("stream MemoryManager.after_turn 失败: %s", exc)
        except Exception as exc:
            logger.exception("stream graph 调用失败，降级到 SoulLoader")
            degraded = True
            # 降级路径：用 SoulLoader.default_soul() 而非硬编码
            if not llm_client.api_key:
                wfile.write(
                    b"event: error\ndata: "
                    + json.dumps({"error": "LLM API key 未配置"}).encode()
                    + b"\n\n"
                )
                wfile.flush()
                return
            messages = [
                {"role": "system", "content": SoulLoader().default_soul()},
                {"role": "user", "content": query},
            ]
            try:
                response_text = await llm_client.chat(messages, temperature=0.3)
            except Exception as fallback_exc:
                err = json.dumps(
                    {"error": f"服务暂不可用: {fallback_exc}"},
                    ensure_ascii=False,
                )
                wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))
                wfile.flush()
                return

        # 流式推送：按句号/换行/分号切块，模拟 token 级流式
        # 这样既保留了 SSE 的「逐块可见」体验，又确保规则链 100% 生效
        chunks = self._split_for_streaming(response_text)
        for chunk in chunks:
            data = json.dumps(
                {"chunk": chunk, "degraded": degraded, "risk_tier": risk_tier},
                ensure_ascii=False,
            )
            wfile.write(f"data: {data}\n\n".encode("utf-8"))
            wfile.flush()

        # 结束事件：附带 safety_triggered 标记，前端可据此显示危机资源
        done_data = json.dumps(
            {
                "degraded": degraded,
                "risk_tier": risk_tier,
                "safety_triggered": safety_triggered,
                "agent": agent_normalized.replace("_", "-"),
            },
            ensure_ascii=False,
        )
        wfile.write(f"event: done\ndata: {done_data}\n\n".encode("utf-8"))
        wfile.flush()

    @staticmethod
    def _split_for_streaming(text: str) -> list[str]:
        """把完整响应切成适合 SSE 流式推送的小块

        切分规则（按优先级）：
        1. 换行符
        2. 中文句号/问号/叹号（。！？）
        3. 英文句号/问号/叹号（.!?）后跟空格或行尾
        4. 中文分号/逗号（；，）
        5. 兜底：每 120 字符一块

        保留分隔符在块尾，避免前端拼接时丢失标点。
        """
        if not text:
            return [""]
        chunks: list[str] = []
        buf: list[str] = []
        buf_len = 0
        for ch in text:
            buf.append(ch)
            buf_len += 1
            # 命中切分点
            if ch in "\n。！？!?；;" and buf_len >= 4:
                chunks.append("".join(buf))
                buf = []
                buf_len = 0
            elif ch == "," and buf_len >= 12:
                # 英文逗号且当前块已较长，切分
                chunks.append("".join(buf))
                buf = []
                buf_len = 0
            elif buf_len >= 120:
                # 兜底：每 120 字符切一刀
                chunks.append("".join(buf))
                buf = []
                buf_len = 0
        if buf:
            chunks.append("".join(buf))
        return chunks

    # ================================================================
    # Phase 8: 用户认证与会话
    # ================================================================

    def _get_user_store(self):
        """懒加载 UserStore（用 settings.auth_data_dir，便于测试 monkeypatch）"""
        from ..auth.store import UserStore
        return UserStore(data_dir=settings.auth_data_dir)

    def _get_jwt_manager(self):
        """懒加载 JWTManager"""
        from ..auth.jwt import JWTManager
        secret = settings.jwt_secret or None
        return JWTManager(secret=secret, expiry_days=settings.jwt_expiry_days)

    def _require_auth(self, headers: dict) -> dict | None:
        """从 Authorization: Bearer <token> 解析用户

        返回 user dict 或 None（未认证）
        """
        auth_header = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth_header = v
                break
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return None
        token = auth_header[7:].strip()
        if not token:
            return None
        jwt_mgr = self._get_jwt_manager()
        payload = jwt_mgr.verify(token)
        if payload is None:
            return None
        store = self._get_user_store()
        user = store.get_user(payload.get("user_id", ""))
        if user is None:
            return None
        return user

    async def _handle_auth_register(self, body: dict) -> dict:
        """POST /api/auth/register
        body: {email, password, display_name?}
        返回 {user_id, token, expires_at}
        """
        email = body.get("email", "")
        password = body.get("password", "")
        display_name = body.get("display_name")

        store = self._get_user_store()
        # 注册时使用 settings.password_min_length 覆盖默认（若 env 调整过）
        store.password_min_length = settings.password_min_length
        user = store.register(email, password, display_name)

        jwt_mgr = self._get_jwt_manager()
        token = jwt_mgr.issue(user)
        expires_at = self._token_expiry_iso(jwt_mgr, token)
        return {
            "user_id": user["user_id"],
            "token": token,
            "expires_at": expires_at,
        }

    async def _handle_auth_login(self, body: dict) -> dict | None:
        """POST /api/auth/login
        body: {email, password}
        返回 {user_id, token, expires_at, display_name}
        """
        email = body.get("email", "")
        password = body.get("password", "")

        store = self._get_user_store()
        user = store.verify(email, password)
        if user is None:
            return None

        jwt_mgr = self._get_jwt_manager()
        token = jwt_mgr.issue(user)
        expires_at = self._token_expiry_iso(jwt_mgr, token)
        return {
            "user_id": user["user_id"],
            "token": token,
            "expires_at": expires_at,
            "display_name": user.get("display_name", ""),
        }

    async def _handle_auth_me(self, headers: dict) -> dict | None:
        """GET /api/auth/me
        需要 Authorization 头
        返回 {user_id, email, display_name, role, family_id, created_at}
        """
        user = self._require_auth(headers)
        if user is None:
            return None
        return user

    async def _handle_auth_refresh(self, headers: dict) -> dict | None:
        """POST /api/auth/refresh
        需要 Authorization 头
        返回新 token（剩余有效期 < 1 天时）；否则返回 None
        """
        auth_header = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth_header = v
                break
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return None
        token = auth_header[7:].strip()
        if not token:
            return None
        jwt_mgr = self._get_jwt_manager()
        new_token = jwt_mgr.refresh(token)
        if new_token is None:
            return None
        expires_at = self._token_expiry_iso(jwt_mgr, new_token)
        return {"token": new_token, "expires_at": expires_at}

    @staticmethod
    def _token_expiry_iso(jwt_mgr, token: str) -> str:
        """从 token 解析 exp 并转为 ISO 时间戳"""
        from datetime import datetime, timezone
        payload = jwt_mgr.verify(token)
        if payload is None:
            return ""
        exp = payload.get("exp", 0)
        try:
            return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    @staticmethod
    def _parse_multipart(handler) -> dict[str, Any]:
        """极简 multipart/form-data 解析（仅用于 /api/documents/extract）

        支持常见浏览器上传：boundary 分隔的多部分表单，每部分含
        Content-Disposition + 可选 Content-Type。返回 {filename, content, doc_type}。

        不引入新依赖（cgi 在 Python 3.13 已 deprecated）。
        """
        ct = handler.headers.get("Content-Type", "")
        # 提取 boundary
        boundary = None
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip().strip('"')
                break
        if not boundary:
            return {"filename": "", "content": b"", "doc_type": ""}
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length) if length else b""
        boundary_bytes = ("--" + boundary).encode("ascii")
        sections = body.split(boundary_bytes)
        result: dict[str, Any] = {"filename": "", "content": b"", "doc_type": ""}
        for sec in sections:
            if not sec or sec in (b"--", b"--\r\n", b"\r\n"):
                continue
            # 去掉首尾 CRLF
            sec = sec.strip(b"\r\n")
            if not sec:
                continue
            # 分离 headers 和 body（空行分隔）
            header_body_split = sec.split(b"\r\n\r\n", 1)
            if len(header_body_split) != 2:
                continue
            header_bytes, body_bytes = header_body_split
            # 解析 Content-Disposition
            name = ""
            filename = ""
            for line in header_bytes.split(b"\r\n"):
                line_str = line.decode("utf-8", errors="ignore")
                if line_str.lower().startswith("content-disposition:"):
                    # 提取 name="..." 和 filename="..."
                    for tok in line_str.split(";"):
                        tok = tok.strip()
                        if tok.startswith("name="):
                            name = tok[len("name="):].strip().strip('"')
                        elif tok.startswith("filename="):
                            filename = tok[len("filename="):].strip().strip('"')
            # 去掉 body 末尾的 \r\n（multipart 协议要求）
            if body_bytes.endswith(b"\r\n"):
                body_bytes = body_bytes[:-2]
            if name == "file":
                result["filename"] = filename
                result["content"] = body_bytes
            elif name == "doc_type":
                result["doc_type"] = body_bytes.decode("utf-8", errors="ignore")
        return result


# 全局单例
web_server = WebServer()

# 向后兼容别名（验证脚本使用 DeadmanWebServer 名称导入）
DeadmanWebServer = WebServer


def main() -> None:
    """命令行入口：启动 Web Server"""
    import argparse

    parser = argparse.ArgumentParser(prog="deadman-web-server", description="AG-UI Web Server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    web_server.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
