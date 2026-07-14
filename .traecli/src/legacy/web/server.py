"""AG-UI Web Server - 提供对话界面 + chat API + SSE 流式

端点：
  GET  /                   -> 对话界面（index.html）
  GET  /api/health         -> 健康检查
  POST /api/chat           -> 同步对话（返回完整响应）
  GET  /api/stream?query=  -> SSE 流式对话（逐 token 推送）
  GET  /api/agents         -> 智能体列表
  GET  /api/tools          -> MCP 工具列表

前端：web/static/index.html（单页应用，原生 JS，无构建依赖）
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import settings

logger = logging.getLogger(__name__)

# 静态文件目录
_STATIC_DIR = Path(__file__).parent / "static"


class WebServer:
    """AG-UI Web Server

    提供对话界面和 API 端点，与 MCP Server / A2A Server 共存。
    """

    def __init__(self) -> None:
        self.host = settings.mcp_server_host
        # Web UI 端口默认比 MCP +2（MCP=8000, A2A=8001, Web=8002）
        self.port = int(sys.getenv("WEB_SERVER_PORT", "8002"))

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

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if path == "/" or path == "/index.html":
                    self._send_file(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
                elif path == "/api/health":
                    self._send_json(200, {"status": "ok", "service": "ag-ui"})
                elif path == "/api/stream":
                    self._handle_stream(query)
                elif path == "/api/agents":
                    self._handle_agents()
                elif path == "/api/tools":
                    self._handle_tools()
                elif path == "/metrics":
                    self._handle_metrics()
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
                if path != "/api/chat":
                    self.send_error(404, "Not Found")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return
                resp = asyncio.run(server_ref._handle_chat(req))
                self._send_json(200, resp)

            def _handle_stream(self, query: dict[str, list[str]]) -> None:
                """SSE 流式对话"""
                q = query.get("query", [""])[0]
                agent = query.get("agent", ["death-aftercare"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    asyncio.run(
                        server_ref._stream_chat(self.wfile, q, agent)
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

        httpd = ThreadingHTTPServer((host, port), Handler)
        logger.info("AG-UI Web Server listening on http://%s:%d", host, port)
        print(f"AG-UI Web Server listening on http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()

    async def _handle_chat(self, req: dict[str, Any]) -> dict[str, Any]:
        """处理同步对话请求"""
        query = req.get("query", "")
        agent = req.get("agent", "death-aftercare")
        history = req.get("history", [])

        if not query:
            return {"error": "query 不能为空"}

        from ..llm import llm_client

        if not llm_client.api_key:
            return {
                "response": (
                    "LLM API key 未配置。请在 .env 中设置 LLM_API_KEY 后重启服务。"
                ),
                "agent": agent,
                "degraded": True,
            }

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"你是 {agent} 智能体，专注于协助处理逝者身后事。"
                    "请用温和、专业的语气回答，给出具体可操作的建议。"
                    "涉及法律/医疗/财务的专业问题，建议用户咨询专业人士。"
                ),
            }
        ]
        # 加入历史对话（最近 10 轮）
        for item in history[-10:]:
            role = item.get("role", "user")
            content = item.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})

        try:
            response = await llm_client.chat(messages, temperature=0.3)
            return {
                "response": response,
                "agent": agent,
                "degraded": False,
            }
        except Exception as exc:
            logger.exception("chat 调用失败")
            return {
                "response": f"调用失败: {exc}",
                "agent": agent,
                "degraded": True,
                "error": str(exc),
            }

    async def _stream_chat(self, wfile: Any, query: str, agent: str) -> None:
        """SSE 流式推送对话"""
        from ..llm import llm_client

        if not query:
            wfile.write(
                b"event: error\ndata: "
                + json.dumps({"error": "query 不能为空"}).encode()
                + b"\n\n"
            )
            wfile.flush()
            return

        if not llm_client.api_key:
            wfile.write(
                b"event: error\ndata: "
                + json.dumps({"error": "LLM API key 未配置"}).encode()
                + b"\n\n"
            )
            wfile.flush()
            return

        messages = [
            {
                "role": "system",
                "content": (
                    f"你是 {agent} 智能体，专注于协助处理逝者身后事。"
                    "请用温和、专业的语气回答。"
                ),
            },
            {"role": "user", "content": query},
        ]

        try:
            async for chunk in llm_client.chat_stream(messages, temperature=0.3):
                data = json.dumps({"chunk": chunk}, ensure_ascii=False)
                wfile.write(f"data: {data}\n\n".encode("utf-8"))
                wfile.flush()
            # 结束事件
            wfile.write(b"event: done\ndata: {}\n\n")
            wfile.flush()
        except Exception as exc:
            err = json.dumps({"error": str(exc)}, ensure_ascii=False)
            wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))
            wfile.flush()


# 全局单例
web_server = WebServer()


def main() -> None:
    """命令行入口：启动 Web Server"""
    import argparse

    parser = argparse.ArgumentParser(prog="legacy-web-server", description="AG-UI Web Server")
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
