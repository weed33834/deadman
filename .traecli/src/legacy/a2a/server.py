"""A2A v1.0 Server - AgentCard 发布 + tasks/send 处理

端点：
  GET  /.well-known/agent.json     -> 返回 AgentCard
  POST /a2a                        -> JSON-RPC 2.0（tasks/send, tasks/get）
  POST /a2a/subscribe              -> SSE 流式更新（tasks/sendSubscribe）

用标准库 http.server 实现，与 MCP Server 保持一致的降级策略。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ..config import settings
from .models import A2ATask, AgentCard, AgentCardSkill, TaskState

logger = logging.getLogger(__name__)


def _build_default_card() -> AgentCard:
    """构建本平台的默认 AgentCard

    声明 6 个核心能力（对应 6 个并列智能体）。
    """
    return AgentCard(
        name="legacy-aftercare-platform",
        description="身后事多智能体平台 - 协助处理逝者身后事全流程",
        version="4.4.1",
        url=f"http://{settings.mcp_server_host}:{settings.mcp_server_port}/a2a",
        skills=[
            AgentCardSkill(
                id="death-aftercare",
                name="身后事流程引导",
                description="死亡证明、户口注销、数字账号、遗产继承 9 阶段全流程",
                tags=["aftercare", "death", "inheritance"],
                jurisdictions=["CN", "US-CA", "JP"],
            ),
            AgentCardSkill(
                id="legal-advisor",
                name="法律咨询",
                description="继承法、遗产分配、跨境法律问题",
                tags=["legal", "law", "inheritance"],
                jurisdictions=["CN", "US", "JP"],
            ),
            AgentCardSkill(
                id="financial-analyst",
                name="财务分析",
                description="资产清查、税务规划、保险理赔",
                tags=["financial", "tax", "insurance"],
                jurisdictions=["CN", "US"],
            ),
            AgentCardSkill(
                id="policy-researcher",
                name="政策研究",
                description="各地身后事政策调研，包括丧葬补贴、社保结算",
                tags=["policy", "research"],
                jurisdictions=["CN"],
            ),
            AgentCardSkill(
                id="cross-border-specialist",
                name="跨境事务",
                description="跨国遗产继承、遗体运输、国际法律冲突",
                tags=["cross-border", "international"],
                jurisdictions=["CN", "US", "JP"],
            ),
            AgentCardSkill(
                id="medical-guide",
                name="医疗导航",
                description="临终医疗决策、医保报销、医疗文书",
                tags=["medical", "healthcare"],
                jurisdictions=["CN"],
            ),
        ],
        provider={
            "name": "Legacy Aftercare Platform",
            "url": "https://github.com/bad-hope/legacy-aftercare",
        },
        authentication={"schemes": ["bearer"]},
    )


class A2AServer:
    """A2A v1.0 Server

    处理 JSON-RPC 2.0 请求，管理任务生命周期。
    可与 MCP Server 共用端口，也可独立运行。
    """

    def __init__(self, card: AgentCard | None = None):
        self.card = card or _build_default_card()
        # 任务存储（进程内 dict；生产环境可换 Redis/DB）
        self._tasks: dict[str, A2ATask] = {}

    def get_card(self) -> dict[str, Any]:
        """返回 AgentCard"""
        return self.card.to_dict()

    async def handle_jsonrpc(self, req: dict[str, Any]) -> dict[str, Any]:
        """处理 JSON-RPC 2.0 请求"""
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {}) or {}

        try:
            if method == "tasks/send":
                return await self._tasks_send(req_id, params)
            if method == "tasks/get":
                return self._tasks_get(req_id, params)
            if method == "tasks/cancel":
                return self._tasks_cancel(req_id, params)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            logger.exception("A2A JSON-RPC 处理失败: %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc), "data": type(exc).__name__},
            }

    async def _tasks_send(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tasks/send - 接收任务并执行

        params:
          - skill_id: 调用的能力 ID
          - message: {role: "user", parts: [{type: "text", content: "..."}]}
          - metadata: 可选元数据
        """
        skill_id = params.get("skill_id", "")
        message = params.get("message", {})
        metadata = params.get("metadata", {})

        # 创建任务
        task_id = str(uuid.uuid4())
        task = A2ATask(
            id=task_id,
            state=TaskState.SUBMITTED,
            message=message,
            metadata=metadata,
        )
        self._tasks[task_id] = task

        # 验证 skill_id 存在
        skill = next((s for s in self.card.skills if s.id == skill_id), None)
        if skill is None:
            task.state = TaskState.FAILED
            task.error = f"未知 skill_id: {skill_id}"
            return {"jsonrpc": "2.0", "id": req_id, "result": task.to_dict()}

        # 执行任务（调用 LLM）
        task.state = TaskState.WORKING
        try:
            # 提取用户消息文本
            user_text = ""
            if isinstance(message, dict):
                parts = message.get("parts", [])
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "text":
                        user_text += part.get("content", "")

            # 调用 LLM
            from ..llm import llm_client

            if llm_client.api_key:
                response = await llm_client.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                f"你是 {skill.name}。{skill.description}。"
                                f"适用地区: {', '.join(skill.jurisdictions)}。"
                                "请基于你的专业知识回答用户问题。"
                            ),
                        },
                        {"role": "user", "content": user_text},
                    ],
                    temperature=0.3,
                )
                task.result = {
                    "role": "agent",
                    "parts": [{"type": "text", "content": response}],
                }
                task.state = TaskState.COMPLETED
            else:
                task.state = TaskState.FAILED
                task.error = "LLM API key 未配置，无法执行任务"
        except Exception as exc:
            task.state = TaskState.FAILED
            task.error = f"{type(exc).__name__}: {exc}"

        return {"jsonrpc": "2.0", "id": req_id, "result": task.to_dict()}

    def _tasks_get(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tasks/get - 查询任务状态"""
        task_id = params.get("task_id", "")
        task = self._tasks.get(task_id)
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"任务不存在: {task_id}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": task.to_dict()}

    def _tasks_cancel(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tasks/cancel - 取消任务"""
        task_id = params.get("task_id", "")
        task = self._tasks.get(task_id)
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"任务不存在: {task_id}"},
            }
        task.state = TaskState.CANCELED
        return {"jsonrpc": "2.0", "id": req_id, "result": task.to_dict()}

    def run(self, host: str | None = None, port: int | None = None) -> None:
        """启动 A2A HTTP Server"""
        host = host or settings.mcp_server_host
        port = port or (settings.mcp_server_port + 1)  # 默认比 MCP 端口 +1
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug("A2A HTTP %s - %s", self.address_string(), format % args)

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                # AgentCard endpoint
                if self.path == "/.well-known/agent.json":
                    self._send_json(200, server_ref.get_card())
                elif self.path == "/a2a/health":
                    self._send_json(200, {"status": "ok", "card": server_ref.card.name})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/a2a":
                    self._send_json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return
                resp = asyncio.run(server_ref.handle_jsonrpc(req))
                self._send_json(200, resp)

        httpd = ThreadingHTTPServer((host, port), Handler)
        logger.info(
            "A2A Server listening on http://%s:%d/.well-known/agent.json", host, port
        )
        print(f"A2A Server listening on http://{host}:{port}/.well-known/agent.json")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()


# 全局单例
a2a_server = A2AServer()


def main() -> None:
    """命令行入口：启动 A2A Server"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="legacy-a2a-server", description="A2A v1.0 Server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    a2a_server.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
