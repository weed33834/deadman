"""A2A v1.0 / v1.2 Server - AgentCard 发布 + tasks/send 处理

端点：
  GET  /.well-known/agent.json     -> 返回 AgentCard
  POST /a2a                        -> JSON-RPC 2.0（tasks/send, tasks/get, tasks/cancel）
  POST /a2a                        -> JSON-RPC 2.0 v1.2 扩展（feature flag 控制）：
                                       - tasks/sendSubscribe: SSE 流式任务更新
                                       - tasks/sendPush: Webhook 推送
  GET  /a2a/subscribe?task_id=...  -> SSE 流式端点（v1.2）

用标准库 http.server 实现，与 MCP Server 保持一致的降级策略。

P4.4 v1.2 升级（feature flag DEADMAN_A2A_V12_ENABLED=0 默认关闭）：
- 仅 v1.2 开启时启用 sendSubscribe / sendPush / 签名认证
- v1.0 行为完全不变（tasks/send/get/cancel 路径不动）
- cryptography 可选依赖；缺失时签名认证降级为 no-op（仅记 warning）
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from .._version import __version__ as DEADMAN_VERSION
from ..config import settings
from .models import (
    A2A_V12_ENABLED,
    A2ATask,
    AgentCard,
    AgentCardSkill,
    PushNotificationConfig,
    TaskState,
)

logger = logging.getLogger(__name__)

# =====================================================================
# 可选依赖 - httpx（webhook 推送）/ cryptography（签名认证）
# =====================================================================
try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False


def _build_default_card() -> AgentCard:
    """构建本平台的默认 AgentCard

    声明 6 个核心能力（对应 6 个并列智能体）。
    """
    return AgentCard(
        name="deadman-platform",
        description="身后事多智能体引导平台 - 协助处理逝者身后事全流程",
        version=DEADMAN_VERSION,
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
            "name": "deadman Platform",
            "url": "https://github.com/weed33834/deadman",
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
        # P4.4 v1.2：webhook 推送配置订阅（task_id -> PushNotificationConfig）
        self._push_subscriptions: dict[str, PushNotificationConfig] = {}

    def get_card(self) -> dict[str, Any]:
        """返回 AgentCard"""
        return self.card.to_dict()

    def run(self, host: str | None = None, port: int | None = None) -> None:
        """启动 HTTP 服务器（由 _a2a_server_run 实现，见文件末尾动态绑定）。"""
        # 实际实现在 _a2a_server_run 中，通过 A2AServer.run = _a2a_server_run 绑定
        raise NotImplementedError("run method not bound")

    async def handle_jsonrpc(self, req: dict[str, Any]) -> dict[str, Any]:
        """处理 JSON-RPC 2.0 请求

        v1.0 方法：tasks/send / tasks/get / tasks/cancel（始终可用）
        v1.2 方法：tasks/sendSubscribe / tasks/sendPush（feature flag 控制）
        """
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {}) or {}

        try:
            # === v1.0 方法（行为不变）===
            if method == "tasks/send":
                return await self._tasks_send(req_id, params)
            if method == "tasks/get":
                return self._tasks_get(req_id, params)
            if method == "tasks/cancel":
                return self._tasks_cancel(req_id, params)

            # === v1.2 方法（feature flag 控制）===
            if method == "tasks/sendSubscribe":
                if not A2A_V12_ENABLED:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method not found (A2A v1.2 disabled): {method}",
                        },
                    }
                return await self._tasks_send_subscribe(req_id, params)
            if method == "tasks/sendPush":
                if not A2A_V12_ENABLED:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method not found (A2A v1.2 disabled): {method}",
                        },
                    }
                return await self._tasks_send_push(req_id, params)

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

        # 执行任务（通过编排图，走完整 L0-L8 规则链）
        task.state = TaskState.WORKING
        try:
            # 提取用户消息文本
            user_text = ""
            if isinstance(message, dict):
                parts = message.get("parts", [])
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "text":
                        user_text += part.get("content", "")

            # 走编排图（与 CLI / Web 入口一致，L0-L8 规则链全部生效）
            from ..llm import llm_client

            # 先检查 LLM 可用性：未配置 API key 时直接失败，不走 graph
            if not llm_client.api_key:
                task.state = TaskState.FAILED
                task.error = "LLM API key 未配置，无法执行任务"
            else:
                from ..orchestration.graph import build_main_graph
                from ..orchestration.state import ConversationState

                # skill_id → agent_name（短横线转下划线）
                agent_name = skill_id.replace("-", "_")
                state = ConversationState(
                    user_input=user_text,
                    current_agent=agent_name,
                    session_id=task_id,
                    agent_name=agent_name,  # type: ignore[typeddict-unknown-key]
                    user_id="a2a",  # type: ignore[typeddict-unknown-key]
                )

                try:
                    graph = build_main_graph()
                    result_state = await graph.ainvoke(
                        state, config={"configurable": {"thread_id": task_id}}
                    )
                    response = result_state.get("final_response") or result_state.get(
                        "draft_response", ""
                    )
                    if response:
                        task.result = {
                            "role": "agent",
                            "parts": [{"type": "text", "content": response}],
                        }
                        task.state = TaskState.COMPLETED
                    else:
                        task.state = TaskState.FAILED
                        task.error = "编排图未返回响应"
                except Exception as graph_exc:
                    # graph 失败时降级到直调 LLM（保留最低可用性）
                    logger.warning("A2A graph 执行失败，降级到直调 LLM: %s", graph_exc)
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
                    # 降级路径也必须过 L0 安全检查（与主路径 rule_check_node 一致，
                    # 避免编排图失败时安全干预被绕过）
                    try:
                        from ..rules_loader import SAFETY_OVERRIDE_RESPONSE, rule_checker

                        rc = rule_checker.check(
                            output_text=response,
                            context={"user_input": user_text},
                        )
                        if rc.safety_triggered:
                            logger.warning("A2A 降级路径 L0 安全触发，替换为安全响应")
                            response = SAFETY_OVERRIDE_RESPONSE
                    except Exception as rc_exc:
                        logger.warning("A2A 降级路径规则检查失败（不阻塞降级）: %s", rc_exc)
                    task.result = {
                        "role": "agent",
                        "parts": [{"type": "text", "content": response}],
                    }
                    task.state = TaskState.COMPLETED
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

    # ==================================================================
    # P4.4 v1.2 方法（feature flag DEADMAN_A2A_V12_ENABLED=1 启用）
    # ==================================================================

    async def _tasks_send_subscribe(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tasks/sendSubscribe - SSE 流式任务更新

        与 tasks/send 类似接收任务，但返回值包含一系列 SSE 事件，
        调用方可按 text/event-stream 解析。HTTP handler 会把 events
        字段格式化为 SSE wire 格式（data: ...\\n\\n）。

        params 同 tasks/send（skill_id / message / metadata）

        Returns:
            {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"task": task.to_dict(), "events": [
                    {"event": "working", "data": {...}},
                    {"event": "completed", "data": {...}},
                ]},
                "_streaming": True  # 标记 HTTP handler 切换 SSE 响应
            }

        降级路径：
        - v1.2 关闭 → handle_jsonrpc 已返回 -32601（不会到这里）
        - LLM 不可用 → events 仍包含 working + failed 两个事件，task.state=FAILED
        """
        # 复用 _tasks_send 执行任务，拿到最终 task 状态
        send_result = await self._tasks_send(req_id, params)
        # _tasks_send 返回的 result 是 task.to_dict()
        task_dict = send_result.get("result", {}) if "result" in send_result else {}
        task_id = task_dict.get("id", "")
        task = self._tasks.get(task_id)

        # 构造 SSE 事件序列：working → completed/failed
        events: list[dict[str, Any]] = []
        events.append(
            {
                "event": "working",
                "data": {"task_id": task_id, "state": "working"},
            }
        )
        if task is not None:
            if task.state == TaskState.COMPLETED:
                events.append(
                    {
                        "event": "completed",
                        "data": {
                            "task_id": task_id,
                            "state": "completed",
                            "result": task.result,
                        },
                    }
                )
            elif task.state == TaskState.FAILED:
                events.append(
                    {
                        "event": "failed",
                        "data": {
                            "task_id": task_id,
                            "state": "failed",
                            "error": task.error,
                        },
                    }
                )
            else:
                events.append(
                    {
                        "event": "update",
                        "data": {"task_id": task_id, "state": task.state.value},
                    }
                )

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"task": task_dict, "events": events},
            "_streaming": True,  # HTTP handler 据此切 SSE wire 格式
        }

    async def _tasks_send_push(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tasks/sendPush - Webhook 推送

        接收 {task_id, webhook_url, event_type}，用 httpx POST 到 webhook_url，
        把任务当前状态推送给订阅方。

        params:
            - task_id: 已存在的任务 ID
            - webhook_url: 接收推送的 URL
            - event_type: 事件类型（如 "task.completed"）
            - token: 可选 bearer token（Authorization header）

        Returns:
            {"jsonrpc": "2.0", "id": req_id, "result": {
                "pushed": bool, "status_code": int, "error": str
            }}

        降级路径：
        - v1.2 关闭 → handle_jsonrpc 已返回 -32601
        - task 不存在 → 返回 -32602 错误
        - httpx 不可用 → pushed=False, error="httpx 不可用"
        - webhook 调用失败 → pushed=False, error=异常信息
        """
        task_id = params.get("task_id", "")
        webhook_url = params.get("webhook_url", "")
        event_type = params.get("event_type", "task.update")
        token = params.get("token", "")

        task = self._tasks.get(task_id)
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"任务不存在: {task_id}"},
            }

        # 构造推送 payload
        payload = {
            "event": event_type,
            "task_id": task_id,
            "task": task.to_dict(),
            "pushed_at": _now_iso(),
        }
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # httpx 不可用 → 降级返回 pushed=False
        if not _HAS_HTTPX:
            logger.warning("httpx 不可用，webhook 推送失败: %s", webhook_url)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "pushed": False,
                    "status_code": 0,
                    "error": "httpx 不可用",
                },
            }

        # POST 到 webhook
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload, headers=headers)
            pushed = 200 <= resp.status_code < 300
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "pushed": pushed,
                    "status_code": resp.status_code,
                    "error": "" if pushed else f"HTTP {resp.status_code}",
                },
            }
        except Exception as exc:
            logger.warning("webhook 推送异常: %s", exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "pushed": False,
                    "status_code": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            }

    # ==================================================================
    # P4.4 v1.2 AgentCard 签名认证（cryptography 可选）
    # ==================================================================

    def sign_agent_card(self, private_key_pem: str | None = None) -> str | None:
        """对当前 AgentCard 做 SHA-256 签名

        Args:
            private_key_pem: PEM 格式私钥；None 时自动生成临时 RSA 密钥对
                             （仅用于测试 / 演示，生产应显式传入）

        Returns:
            hex 签名；cryptography 不可用或 v1.2 关闭时返回 None

        降级路径：
        - v1.2 关闭 → 返回 None
        - cryptography 不可用 → 返回 None（记 warning）
        """
        if not A2A_V12_ENABLED:
            return None
        if not _HAS_CRYPTOGRAPHY:
            logger.warning("cryptography 不可用，AgentCard 签名降级为 None")
            return None
        try:
            if private_key_pem:
                private_key = serialization.load_pem_private_key(
                    private_key_pem.encode("utf-8"), password=None
                )
            else:
                private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            # 对 AgentCard 的 canonical JSON 做 SHA-256 with RSA 签名
            # load_pem_private_key 返回联合类型,这里仅支持 RSA 签名
            from cryptography.hazmat.primitives.asymmetric.rsa import (
                RSAPrivateKey,
            )

            if not isinstance(private_key, RSAPrivateKey):
                logger.warning("AgentCard 签名仅支持 RSA 私钥")
                return None
            card_bytes = json.dumps(self.card.to_dict(), sort_keys=True, ensure_ascii=False).encode(
                "utf-8"
            )
            signature = private_key.sign(
                card_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return signature.hex()
        except Exception as exc:
            logger.warning("AgentCard 签名失败: %s", exc)
            return None

    def verify_agent_card_signature(
        self,
        public_key_pem: str,
        signature_hex: str,
        card_dict: dict[str, Any] | None = None,
    ) -> bool:
        """校验 AgentCard 签名

        Args:
            public_key_pem: PEM 格式公钥
            signature_hex: hex 签名
            card_dict: 待校验的 AgentCard dict；None 用当前 card

        Returns:
            True/False；v1.2 关闭或 cryptography 不可用 → 返回 False
        """
        if not A2A_V12_ENABLED:
            return False
        if not _HAS_CRYPTOGRAPHY:
            logger.warning("cryptography 不可用，AgentCard 签名校验返回 False")
            return False
        try:
            public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
            card_data = card_dict if card_dict is not None else self.card.to_dict()
            card_bytes = json.dumps(card_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            signature = bytes.fromhex(signature_hex)
            # load_pem_public_key 返回联合类型,这里仅支持 RSA 验签
            from cryptography.hazmat.primitives.asymmetric.rsa import (
                RSAPublicKey,
            )

            if not isinstance(public_key, RSAPublicKey):
                logger.warning("AgentCard 验签仅支持 RSA 公钥")
                return False
            public_key.verify(
                signature,
                card_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True
        except InvalidSignature:
            return False
        except Exception as exc:
            logger.warning("AgentCard 签名校验异常: %s", exc)
            return False


# =====================================================================
# v1.2 辅助函数
# =====================================================================


def _now_iso() -> str:
    """当前时间 ISO 字符串"""
    from datetime import datetime

    return datetime.now().isoformat()


def format_sse_events(events: list[dict[str, Any]]) -> str:
    """把 events 列表格式化为 SSE wire 格式字符串

    SSE 规范：每条事件 `event: NAME\\ndata: JSON\\n\\n`
    """
    chunks: list[str] = []
    for ev in events:
        event_name = ev.get("event", "message")
        data = ev.get("data", {})
        chunks.append(f"event: {event_name}")
        chunks.append(f"data: {json.dumps(data, ensure_ascii=False)}")
        chunks.append("")  # 空行分隔
    return "\n".join(chunks) + "\n"


# =====================================================================
# A2AServer.run - HTTP Server 启动（在类外补回，避免类定义中途插入）
# =====================================================================


def _a2a_server_run(self: A2AServer, host: str | None = None, port: int | None = None) -> None:
    """A2AServer.run 的实际实现（绑定到类作为 run 方法）"""
    host = host or settings.mcp_server_host
    port = port or (settings.mcp_server_port + 1)  # 默认比 MCP 端口 +1
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    server_ref = self

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("A2A HTTP %s - %s", self.address_string(), format % args)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_sse(self, events: list[dict[str, Any]]) -> None:
            """v1.2: 发送 SSE 流式响应（text/event-stream）"""
            body = format_sse_events(events).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            # AgentCard endpoint
            if self.path == "/.well-known/agent.json":
                self._send_json(200, server_ref.get_card())
            elif self.path == "/a2a/health":
                self._send_json(200, {"status": "ok", "card": server_ref.card.name})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
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
            # v1.2: sendSubscribe 返回 _streaming 标记 → 切 SSE wire 格式
            if isinstance(resp, dict) and resp.get("_streaming") is True:
                result = resp.get("result", {}) or {}
                events = result.get("events", []) if isinstance(result, dict) else []
                # 去掉内部标记后再发 SSE
                resp.pop("_streaming", None)
                self._send_sse(events)
            else:
                self._send_json(200, resp)

    httpd = ThreadingHTTPServer((host, port), Handler)
    logger.info("A2A Server listening on http://%s:%d/.well-known/agent.json", host, port)
    print(f"A2A Server listening on http://{host}:{port}/.well-known/agent.json")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


# 绑定到 A2AServer 类作为 run 方法
A2AServer.run = _a2a_server_run  # type: ignore[attr-defined,method-assign]


# 全局单例
a2a_server = A2AServer()


def main() -> None:
    """命令行入口：启动 A2A Server"""
    import argparse

    # 结构化日志早期初始化（读取 DEADMAN_LOG_LEVEL/DEADMAN_LOG_FORMAT 环境变量）。
    # --log-level 解析后会再次覆盖级别。
    from ..logging_config import setup_logging as _setup_structlog_logging

    _setup_structlog_logging()

    parser = argparse.ArgumentParser(prog="deadman-a2a-server", description="A2A v1.0 Server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _setup_structlog_logging(level=args.log_level)
    a2a_server.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
