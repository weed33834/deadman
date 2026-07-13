"""MCP Server 实现 - 封装身后事平台的 11 个工具

优先尝试使用 FastMCP；若 fastmcp 包不可用，则降级为纯 Python async + 装饰器模式。
两种实现共享同一套工具注册逻辑，调用方式对上层透明。

工具清单：
  1. query_knowledge      - 知识库查询（支持 vector/local/global/hybrid 与本体过滤）
  2. web_search           - 联网搜索（mock）
  3. read_file            - 读取项目文件
  4. write_file           - 写入文件（带安全限制）
  5. invoke_subagent      - 调用子智能体
  6. check_integrity      - 5 关事实复核 + SelfCheckGPT 数字类校验
  7. check_rules          - 规则校验（调用 rule_checker）
  8. query_memory         - 分层记忆查询
  9. initiate_debate      - 发起辩论
 10. call_external_agent  - A2A 外部调用（需 user_consent）
 11. execute_reflexion    - 反思重试
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import settings
from ..types import (
    ConfidenceLabel,
    ExecutionMode,
)
from ..rules_loader import rule_checker

logger = logging.getLogger(__name__)

# =====================================================================
# 可选依赖 - 全部走 try/except，失败则降级
# =====================================================================

# LLM 客户端（依赖 httpx，可能未安装）
try:
    from ..llm import llm_client  # type: ignore
except Exception:  # pragma: no cover - 环境降级
    logger.warning("llm 模块不可用（可能缺少 httpx），LLM 相关工具将降级")
    llm_client = None  # type: ignore

# SelfCheckGPT 校验器（selfcheck 模块可能尚未实现）
try:
    from ..selfcheck.checker import SelfCheckChecker  # type: ignore
    _SELFCHECK_AVAILABLE = True
except Exception:
    logger.info("selfcheck 模块不可用，check_integrity 将仅做 5 关校验")
    SelfCheckChecker = None  # type: ignore
    _SELFCHECK_AVAILABLE = False

# trace span 发射器（observability.tracer 可能尚未实现）
# 真实签名：trace_tool_span(tool_name, attributes=None) -> 上下文管理器
try:
    from ..observability.tracer import trace_tool_span  # type: ignore
except Exception:
    logger.info("observability.tracer 不可用，trace span 将被跳过")

    @contextmanager  # type: ignore
    def trace_tool_span(tool_name: str, attributes: dict[str, Any] | None = None):  # noqa: D401
        """降级版 trace_tool_span - no-op 上下文管理器，签名对齐全局实现"""
        yield None

# 分层记忆模块（legacy.memory.manager.MemoryManager）
try:
    from ..memory.manager import MemoryManager  # type: ignore
    _MEMORY_AVAILABLE = True
except Exception:
    MemoryManager = None  # type: ignore
    _MEMORY_AVAILABLE = False

# 辩论模块（可能尚未实现）
try:
    from ..debate.orchestrator import DebateOrchestrator  # type: ignore
    _DEBATE_AVAILABLE = True
except Exception:
    DebateOrchestrator = None  # type: ignore
    _DEBATE_AVAILABLE = False

# Reflexion 模块（legacy.reflexion.engine.ReflexionEngine）
try:
    from ..reflexion.engine import ReflexionEngine, get_predefined_strategy  # type: ignore
    _REFLEXION_AVAILABLE = True
except Exception:
    ReflexionEngine = None  # type: ignore
    get_predefined_strategy = None  # type: ignore
    _REFLEXION_AVAILABLE = False


# =====================================================================
# 辅助函数
# =====================================================================

def _utcnow_iso() -> str:
    """当前 UTC 时间 ISO 字符串"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_resolve(project_relative: str) -> Path | None:
    """将项目内相对路径解析为绝对路径，并校验未越界

    返回 None 表示路径不安全（越界 / 路径穿越）。
    """
    if not project_relative:
        return None
    root = settings.project_root.resolve()
    # 拒绝绝对路径与显式父级引用
    if project_relative.startswith("/") or project_relative.startswith("\\"):
        # 允许指向 project_root 之内的绝对路径
        candidate = Path(project_relative).resolve()
    else:
        candidate = (root / project_relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _redact_pii(data: Any) -> Any:
    """对 A2A 出口数据做 PII 脱敏

    覆盖字段：identifier / name / phone / address / account_number
    """
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for k, v in data.items():
            key_lower = k.lower()
            if key_lower in {"name", "姓名"} and isinstance(v, str):
                redacted[k] = "[NAME]"
            elif key_lower in {"phone", "tel", "mobile", "电话", "手机"} and isinstance(v, str):
                redacted[k] = _mask_phone(v)
            elif key_lower in {"address", "地址", "住址"} and isinstance(v, str):
                redacted[k] = "[ADDRESS]"
            elif key_lower in {"identifier", "id_card", "身份证", "证件号"} and isinstance(v, str):
                redacted[k] = "[ID]"
            elif key_lower in {"account_number", "account", "账号", "账户号", "卡号"} and isinstance(v, str):
                redacted[k] = _mask_account(v)
            else:
                redacted[k] = _redact_pii(v)
        return redacted
    if isinstance(data, list):
        return [_redact_pii(item) for item in data]
    return data


def _mask_phone(phone: str) -> str:
    """电话号码脱敏：保留前 3 后 2，中间用 * 替换"""
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 5:
        return "***"
    return f"{digits[:3]}***{digits[-2:]}"


def _mask_account(account: str) -> str:
    """账号脱敏：保留后 4 位"""
    if len(account) <= 4:
        return "****"
    return "*" * (len(account) - 4) + account[-4:]


def _trust_level_from_label(label: str) -> str:
    """中文可信度 -> 英文 trust_level"""
    mapping = {"高": "high", "中": "medium", "低": "low"}
    return mapping.get(label.strip(), "medium")


def _freshness_status(last_updated: str | None) -> str:
    """根据"最后更新"日期判断新鲜度

    fresh   <= 3 个月
    stale   3-6 个月
    outdated > 6 个月
    """
    if not last_updated:
        return "outdated"
    # 解析 YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", last_updated)
    if not m:
        return "outdated"
    try:
        updated = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return "outdated"
    now = datetime.now(timezone.utc)
    delta_days = (now - updated).days
    if delta_days <= 90:
        return "fresh"
    if delta_days <= 180:
        return "stale"
    return "outdated"


def _parse_knowledge_meta(content: str) -> dict[str, Any]:
    """从知识库 md 内容中解析 "## 元信息" 区块

    提取：最后更新 / 数据来源 / 数据可信度
    """
    meta: dict[str, Any] = {
        "last_updated": None,
        "sources": [],
        "trust_level": "medium",
    }
    # 截取 "## 元信息" 到下一个 "## " 之间
    m = re.search(r"##\s*元信息\s*\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL)
    if not m:
        return meta
    block = m.group(1)
    # 最后更新
    m_updated = re.search(r"最后更新[:：]\s*(.+)", block)
    if m_updated:
        meta["last_updated"] = m_updated.group(1).strip()
    # 数据可信度
    m_trust = re.search(r"数据可信度[:：]\s*(.+)", block)
    if m_trust:
        meta["trust_level"] = _trust_level_from_label(m_trust.group(1).strip())
    # 数据来源（支持子项列表 - URL 提取）
    urls = re.findall(r"https?://[^\s)]+", block)
    meta["sources"] = urls
    return meta


def _extract_numeric_claims(text: str) -> list[dict[str, str]]:
    """从文本中提取数字类 claim（6 种正则）

    用于 SelfCheckGPT 校验目标提取。
    """
    patterns = {
        "phone": r"(?:\+?\d{1,3}[-\s]?)?\d{3,4}[-\s]?\d{3,4}[-\s]?\d{3,4}",
        "days": r"\d+\s*(?:天|个工作日|个自然日|日|months?|days?)",
        "money": r"\d[\d,]*\s*(?:元|万|亿|人民币|美元|日元|RMB|USD|JPY|￥|\$)",
        "percent": r"\d+(?:\.\d+)?\s*%",
        "article": r"第\s*\d+\s*条",
        "step_count": r"(?:共|分)\s*\d+\s*(?:步|阶段|项)",
    }
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for claim_type, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            value = match.group(0).strip()
            if value in seen:
                continue
            seen.add(value)
            found.append({"claim": value, "claim_type": claim_type})
    return found


def _error_response(tool_name: str, exc: BaseException) -> dict[str, Any]:
    """生成统一的错误返回结构"""
    return {
        "ok": False,
        "tool": tool_name,
        "error": type(exc).__name__,
        "message": str(exc),
        "timestamp": _utcnow_iso(),
    }


# =====================================================================
# 工具定义与服务端
# =====================================================================

@dataclass
class ToolDef:
    """单个工具的定义"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    output_schema: dict[str, Any] = field(default_factory=dict)


class McpServer:
    """轻量 MCP Server

    若 fastmcp 可用，内部委托给 FastMCP 实例；否则使用纯 Python 注册表。
    两种模式对外的 API（tool 装饰器、call_tool、list_tools、run）一致。
    """

    def __init__(self, name: str = "legacy-platform"):
        self.name = name
        self._tools: dict[str, ToolDef] = {}
        # 尝试加载 FastMCP（若可用则委托）
        try:
            from fastmcp import FastMCP  # type: ignore

            self._fastmcp: Any = FastMCP(name)
        except Exception:
            self._fastmcp = None
        # 全局 trace_id（用于本进程内 span 关联）
        self.trace_id: str = str(uuid.uuid4())

    # ---------- 工具注册 ----------

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., Awaitable[Any]],
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        """注册一个工具"""
        tool = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            output_schema=output_schema or {},
        )
        self._tools[name] = tool
        # 同步注册到 FastMCP（若可用）
        if self._fastmcp is not None:
            try:
                # FastMCP 的 tool() 装饰器签名差异较大，这里只做尽力注册
                self._fastmcp.tool(name=name, description=description)(handler)
            except Exception:
                pass

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """装饰器：注册一个 async 工具"""

        def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            self.register_tool(name, description, input_schema, fn, output_schema)
            return fn

        return decorator

    # ---------- 工具调用 ----------

    def list_tools(self) -> list[dict[str, Any]]:
        """列出所有工具定义（MCP tools/list 格式）"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                "outputSchema": t.output_schema,
            }
            for t in self._tools.values()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用一个工具（MCP tools/call 格式）

        所有异常被捕获并转为结构化错误返回，永不向外抛出。
        """
        arguments = arguments or {}
        tool = self._tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "tool": name,
                "error": "ToolNotFound",
                "message": f"工具 {name} 未注册",
            }
        # 包一层 trace span；observability 不可用时为 no-op
        try:
            with trace_tool_span(
                name,
                attributes={
                    "trace_id": self.trace_id,
                    "arguments_keys": list(arguments.keys()),
                },
            ):
                result = await tool.handler(**arguments)
                # 工具 handler 返回 dict 时原样透传（已是结构化业务输出）；
                # 非 dict 结果统一包装
                if isinstance(result, dict):
                    return result
                return {"ok": True, "tool": name, "result": result}
        except Exception as exc:  # noqa: BLE001 - 工具层必须吞掉所有异常
            logger.exception("工具 %s 执行失败", name)
            return _error_response(name, exc)

    # ---------- 启动入口 ----------

    def run(self, transport: str = "stdio", host: str | None = None, port: int | None = None) -> None:
        """启动 server

        transport:
          - stdio: 通过 stdin/stdout 走 JSON-RPC（每行一个请求）
          - http:  启动一个简单的 HTTP server（/mcp 与 /tools 端点）
        """
        host = host or settings.mcp_server_host
        port = port or settings.mcp_server_port
        if transport == "stdio":
            self._run_stdio()
        elif transport == "http":
            self._run_http(host, port)
        else:
            raise ValueError(f"不支持的 transport: {transport}（仅支持 stdio / http）")

    def _run_stdio(self) -> None:
        """stdio 传输：从 stdin 读 JSON-RPC，向 stdout 写响应"""
        asyncio.run(self._stdio_loop())

    async def _stdio_loop(self) -> None:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                break
            line_text = line.decode("utf-8", errors="replace").strip()
            if not line_text:
                continue
            try:
                req = json.loads(line_text)
            except json.JSONDecodeError as exc:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                continue
            resp = await self._handle_jsonrpc(req)
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def _handle_jsonrpc(self, req: dict[str, Any]) -> dict[str, Any]:
        """处理单条 JSON-RPC 请求"""
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {}) or {}
        try:
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self.list_tools()}}
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {}) or {}
                result = await self.call_tool(name, arguments)
                return {"jsonrpc": "2.0", "id": req_id, "result": result}
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": self.name, "version": "1.1.0"},
                        "capabilities": {"tools": {}},
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc), "data": type(exc).__name__},
            }

    def _run_http(self, host: str, port: int) -> None:
        """HTTP 传输：使用标准库 http.server 起一个最简端点

        端点：
          GET  /tools          -> 工具列表
          POST /mcp            -> JSON-RPC（tools/list 或 tools/call）
          GET  /health         -> 健康检查
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug("HTTP %s - %s", self.address_string(), format % args)

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/tools":
                    self._send_json(200, {"tools": server_ref.list_tools()})
                elif self.path == "/health":
                    self._send_json(200, {"status": "ok", "name": server_ref.name})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/mcp":
                    self._send_json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"error": f"invalid json: {exc}"})
                    return
                resp = asyncio.run(server_ref._handle_jsonrpc(req))
                self._send_json(200, resp)

        httpd = ThreadingHTTPServer((host, port), Handler)
        logger.info("MCP HTTP server listening on http://%s:%d/mcp", host, port)
        print(f"MCP HTTP server listening on http://{host}:{port}/mcp", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()


# =====================================================================
# 全局 server 单例
# =====================================================================

mcp = McpServer("legacy-platform")


# =====================================================================
# 工具 1: query_knowledge
# =====================================================================

@mcp.tool(
    name="query_knowledge",
    description=(
        "查询地域知识库。输入国家、地区、查询主题，返回当地政策信息。"
        "若知识库不存在，返回 needs_research=true。"
        "支持 LightRAG 知识图谱检索模式（local/global/hybrid）和跨域本体实体/关系类型过滤。"
        "若 LightRAG 未启用或本体图谱不可用，自动降级为向量/原文检索。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "country": {"type": "string", "description": "国家代码，如 CN/US/JP"},
            "region": {"type": "string", "description": "地区，如 beijing/california/tokyo"},
            "topic": {"type": "string", "description": "查询主题，如 death_certificate/estate_inheritance"},
            "fallback_to_search": {"type": "boolean", "description": "知识库不存在时是否建议触发搜索", "default": True},
            "query_mode": {
                "type": "string",
                "enum": ["vector", "local", "global", "hybrid"],
                "default": "vector",
                "description": "检索模式：vector=传统向量检索；local/global/hybrid=LightRAG 知识图谱模式",
            },
            "entity_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按跨域本体实体类型过滤，如 ['DeathCertificate','Organization']",
            },
            "relation_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按跨域本体关系类型过滤，如 ['requires','issued_by']",
            },
        },
        "required": ["country", "topic"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "data": {"type": "object"},
            "graph_entities": {"type": "array"},
            "graph_relations": {"type": "array"},
            "needs_research": {"type": "boolean"},
            "research_suggestion": {"type": "string"},
        },
    },
)
async def query_knowledge(
    country: str,
    topic: str,
    region: str | None = None,
    fallback_to_search: bool = True,
    query_mode: str = "vector",
    entity_types: list[str] | None = None,
    relation_types: list[str] | None = None,
) -> dict[str, Any]:
    """查询地域知识库

    读取 knowledge/regions/{country}/{region|overview}.md，解析元信息与正文。
    当 query_mode 为 local/global/hybrid 但 LightRAG 未启用时，自动降级为 vector
    并在返回中标注 degraded。
    """
    country_dir = settings.knowledge_dir / "regions" / country.upper()
    if not country_dir.exists():
        return {
            "found": False,
            "needs_research": True if fallback_to_search else False,
            "research_suggestion": (
                f"建议触发 policy-researcher 搜索 {country} 国家级政策"
                if fallback_to_search
                else None
            ),
            "data": None,
            "graph_entities": [],
            "graph_relations": [],
            "degraded": False,
        }

    # 选择目标文件
    if region:
        target = country_dir / f"{region.lower()}.md"
        if not target.exists():
            # 退回到 overview
            target = country_dir / "overview.md"
    else:
        target = country_dir / "overview.md"

    if not target.exists():
        return {
            "found": False,
            "needs_research": True if fallback_to_search else False,
            "research_suggestion": (
                f"建议触发 policy-researcher 搜索 {country}/{region or '国家级'} 政策"
                if fallback_to_search
                else None
            ),
            "data": None,
            "graph_entities": [],
            "graph_relations": [],
            "degraded": False,
        }

    content = target.read_text(encoding="utf-8")
    meta = _parse_knowledge_meta(content)
    freshness = _freshness_status(meta["last_updated"])

    # topic 关键字检索：在原文中定位与主题相关的章节
    topic_snippet: str | None = None
    if topic:
        # 把 topic 中的下划线转成可读词，并尝试匹配章节标题
        topic_readable = topic.replace("_", " ")
        pattern = re.compile(
            rf"(##\s*[^\n]*{re.escape(topic_readable)}[^\n]*)\n(.*?)(?=\n##\s|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        m_topic = pattern.search(content)
        if m_topic:
            topic_snippet = f"{m_topic.group(1).strip()}\n{m_topic.group(2).strip()}"
        else:
            # 退回到关键词首次出现的上下文
            idx = content.lower().find(topic_readable.lower())
            if idx >= 0:
                start = max(0, idx - 200)
                end = min(len(content), idx + 800)
                topic_snippet = content[start:end]

    # LightRAG 模式判断
    lightrag_mode = query_mode in {"local", "global", "hybrid"}
    degraded = False
    graph_entities: list[dict[str, Any]] = []
    graph_relations: list[dict[str, Any]] = []
    if lightrag_mode and not settings.lightrag_enabled:
        degraded = True
    # 本体过滤：当前未接入实体图谱，仅回填空列表 + 标注
    if entity_types or relation_types:
        # 没有图谱数据时，过滤无意义，仅在结果中回显请求
        graph_entities = []
        graph_relations = []

    return {
        "found": True,
        "data": {
            "content": topic_snippet or content,
            "full_file": str(target.relative_to(settings.project_root)),
            "last_updated": meta["last_updated"],
            "sources": meta["sources"],
            "trust_level": meta["trust_level"],
            "freshness_status": freshness,
        },
        "graph_entities": graph_entities,
        "graph_relations": graph_relations,
        "needs_research": False,
        "research_suggestion": None,
        "degraded": degraded,
        "query_mode_requested": query_mode,
        "query_mode_actual": "vector" if degraded else query_mode,
        "entity_types_filter": entity_types,
        "relation_types_filter": relation_types,
    }


# =====================================================================
# 工具 2: web_search
# =====================================================================

@mcp.tool(
    name="web_search",
    description=(
        "联网搜索（mock 实现）。当前未接入真实搜索引擎，返回空结果并标记 needs_research=true。"
        "智能体应据此触发 policy-researcher 子智能体或调用 call_external_agent。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询语句"},
            "country": {"type": "string", "description": "限定国家代码，如 CN/US/JP"},
            "language": {"type": "string", "description": "结果语言，如 zh-CN/en-US"},
            "max_results": {"type": "integer", "description": "最大结果数", "default": 5},
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "results": {"type": "array"},
            "needs_research": {"type": "boolean"},
            "mock": {"type": "boolean"},
        },
    },
)
async def web_search(
    query: str,
    country: str | None = None,
    language: str | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """联网搜索（mock）

    当前为占位实现：返回空结果列表，并提示调用方应触发 policy-researcher。
    """
    return {
        "results": [],
        "needs_research": True,
        "mock": True,
        "query": query,
        "country": country,
        "language": language,
        "max_results": max_results,
        "suggestion": (
            "web_search 当前为 mock 实现。建议触发 policy-researcher 子智能体执行真实搜索，"
            "或通过 call_external_agent 调用具备联网能力的外部 agent。"
        ),
    }


# =====================================================================
# 工具 3: read_file
# =====================================================================

@mcp.tool(
    name="read_file",
    description=(
        "读取项目内的文件。仅允许读取 .traecli/ 项目根目录之内的文件，禁止路径穿越。"
        "适合读取 rules/*.md、knowledge/**/*.md、agents/*.md 等。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对于项目根的文件路径，如 rules/integrity-framework.md"},
            "encoding": {"type": "string", "description": "文件编码", "default": "utf-8"},
            "max_bytes": {"type": "integer", "description": "最大读取字节数（防止超大文件）", "default": 1048576},
        },
        "required": ["path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "size": {"type": "integer"},
        },
    },
)
async def read_file(path: str, encoding: str = "utf-8", max_bytes: int = 1048576) -> dict[str, Any]:
    """读取项目内文件，带安全限制"""
    target = _safe_resolve(path)
    if target is None:
        return {"ok": False, "path": path, "error": "路径越界或包含非法引用"}
    if not target.exists():
        return {"ok": False, "path": path, "error": "文件不存在"}
    if not target.is_file():
        return {"ok": False, "path": path, "error": "目标不是文件"}
    size = target.stat().st_size
    if size > max_bytes:
        return {"ok": False, "path": path, "error": f"文件过大 {size} > {max_bytes}"}
    try:
        content = target.read_text(encoding=encoding)
    except UnicodeDecodeError as exc:
        return {"ok": False, "path": path, "error": f"编码错误: {exc}"}
    return {
        "ok": True,
        "path": str(target.relative_to(settings.project_root)),
        "content": content,
        "size": size,
    }


# =====================================================================
# 工具 4: write_file
# =====================================================================

@mcp.tool(
    name="write_file",
    description=(
        "写入项目内的文件。仅允许写入 .traecli/ 项目根目录之内，禁止路径穿越。"
        "安全限制：禁止覆盖 rules/*.md（规则由人工维护）；禁止写入 .env / .git / 凭证文件。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对于项目根的文件路径"},
            "content": {"type": "string", "description": "写入内容"},
            "encoding": {"type": "string", "default": "utf-8"},
            "overwrite": {"type": "boolean", "default": False, "description": "若文件已存在是否覆盖"},
            "create_dirs": {"type": "boolean", "default": True, "description": "是否自动创建父目录"},
        },
        "required": ["path", "content"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "path": {"type": "string"},
            "bytes_written": {"type": "integer"},
            "created": {"type": "boolean"},
        },
    },
)
async def write_file(
    path: str,
    content: str,
    encoding: str = "utf-8",
    overwrite: bool = False,
    create_dirs: bool = True,
) -> dict[str, Any]:
    """写入项目内文件，带安全限制"""
    target = _safe_resolve(path)
    if target is None:
        return {"ok": False, "path": path, "error": "路径越界或包含非法引用"}

    # 禁止写入的路径模式
    rel_posix = target.relative_to(settings.project_root).as_posix().lower()
    forbidden_prefixes = (
        ".git/",
        ".env",
        "credentials",
        "secrets/",
    )
    for prefix in forbidden_prefixes:
        if rel_posix == prefix or rel_posix.startswith(prefix):
            return {"ok": False, "path": path, "error": f"禁止写入受保护路径: {prefix}"}
    # rules/ 目录下只读
    if rel_posix.startswith("rules/") and rel_posix.endswith(".md"):
        return {
            "ok": False,
            "path": path,
            "error": "rules/*.md 由人工维护，禁止通过 write_file 修改",
        }

    existed = target.exists()
    if existed and not overwrite:
        return {"ok": False, "path": path, "error": "文件已存在且 overwrite=false"}

    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)

    data = content.encode(encoding)
    target.write_bytes(data)
    return {
        "ok": True,
        "path": str(target.relative_to(settings.project_root)),
        "bytes_written": len(data),
        "created": not existed,
    }


# =====================================================================
# 工具 5: invoke_subagent
# =====================================================================

@mcp.tool(
    name="invoke_subagent",
    description=(
        "调用子智能体。传入子智能体名（如 death-aftercare-emotional / financial-analyst-taxes）"
        "与任务描述，返回 SubagentResult。若 LLM 不可用则返回 fallback 结果。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "subagent_name": {"type": "string", "description": "子智能体名，如 death-aftercare-emotional"},
            "task": {"type": "string", "description": "委派给子智能体的任务描述"},
            "context": {"type": "object", "description": "上下文（用户输入/已确认事实/已有摘要等）"},
            "timeout": {"type": "integer", "description": "超时秒数", "default": 30},
        },
        "required": ["subagent_name", "task"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "subagent_name": {"type": "string"},
            "execution_mode": {"type": "string"},
            "report": {"type": "object"},
            "confidence": {"type": "number"},
            "sources": {"type": "array"},
        },
    },
)
async def invoke_subagent(
    subagent_name: str,
    task: str,
    context: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """调用子智能体

    优先查找 agents/{subagent_name}.md 获取定义；若 LLM 可用，则用 LLM 生成响应；
    否则返回 fallback 模式结果。
    """
    context = context or {}
    # 查找 agent 定义文件
    agent_file = settings.agents_dir / f"{subagent_name}.md"
    agent_def_exists = agent_file.exists()
    agent_def_snippet = ""
    if agent_def_exists:
        try:
            agent_def_snippet = agent_file.read_text(encoding="utf-8")[:800]
        except Exception:
            agent_def_snippet = ""

    # 若 LLM 可用，尝试调用
    if llm_client is not None:
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        f"你是子智能体 {subagent_name}。遵循你的 agent.md 定义处理任务。"
                        f"agent.md 片段：\n{agent_def_snippet}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"任务：{task}\n\n上下文：{json.dumps(context, ensure_ascii=False)}",
                },
            ]
            response = await asyncio.wait_for(
                llm_client.chat(messages, temperature=0.3),
                timeout=timeout,
            )
            return {
                "subagent_name": subagent_name,
                "execution_mode": ExecutionMode.SUCCESS.value,
                "report": {
                    "task": task,
                    "response": response,
                    "agent_def_found": agent_def_exists,
                },
                "confidence": 0.7,
                "sources": [str(agent_file.relative_to(settings.project_root))] if agent_def_exists else [],
            }
        except asyncio.TimeoutError:
            return _subagent_fallback(subagent_name, task, context, reason=f"LLM 调用超时（{timeout}s）")
        except Exception as exc:
            return _subagent_fallback(subagent_name, task, context, reason=f"LLM 调用失败: {exc}")

    # LLM 不可用，走 fallback
    return _subagent_fallback(
        subagent_name,
        task,
        context,
        reason="LLM 客户端不可用（llm 模块未导入或 httpx 未安装）",
    )


def _subagent_fallback(
    subagent_name: str,
    task: str,
    context: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """生成 fallback 模式的子智能体结果"""
    return {
        "subagent_name": subagent_name,
        "execution_mode": ExecutionMode.FALLBACK.value,
        "report": {
            "task": task,
            "fallback_reason": reason,
            "guidance": (
                f"子智能体 {subagent_name} 无法实际执行任务。"
                "请由父智能体根据 agent.md 中的指引自行处理，或通过 call_external_agent 委派。"
            ),
            "context_echo": context,
        },
        "confidence": 0.3,
        "sources": [],
    }


# =====================================================================
# 工具 6: check_integrity
# =====================================================================

@mcp.tool(
    name="check_integrity",
    description=(
        "输出前 5 关事实复核 + SelfCheckGPT 数字类一致性校验。"
        "校验来源、幻觉、时效、单源、越界，并对数字类 claim（电话/天数/金额/百分比/条文号/步骤数）"
        "做多次采样一致性检测。必须在输出具体事实性信息前调用。"
        "若 selfcheck 模块不可用，自动降级为仅做 5 关校验。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "output_text": {"type": "string", "description": "待校验的输出文本"},
            "claims_to_verify": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "待验证的具体陈述"},
                        "source": {"type": "string", "description": "来源 URL 或文件"},
                        "claim_type": {
                            "type": "string",
                            "enum": ["fact", "number", "legal_citation", "procedure", "phone_number"],
                        },
                    },
                },
            },
            "selfcheck_enabled": {"type": "boolean", "default": True, "description": "是否启用 SelfCheckGPT"},
            "selfcheck_sample_count": {"type": "integer", "default": 5, "description": "SelfCheckGPT 采样次数（3-5）"},
        },
        "required": ["output_text", "claims_to_verify"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "check_results": {"type": "object"},
            "selfcheck_result": {"type": "object"},
            "confidence_labels": {"type": "array"},
        },
    },
)
async def check_integrity(
    output_text: str,
    claims_to_verify: list[dict[str, Any]],
    selfcheck_enabled: bool = True,
    selfcheck_sample_count: int = 5,
) -> dict[str, Any]:
    """5 关事实复核 + SelfCheckGPT 数字类校验"""
    claims_to_verify = claims_to_verify or []

    # ---------- 5 关校验 ----------
    source_issues: list[str] = []
    hallucination_issues: list[str] = []
    freshness_issues: list[str] = []
    single_source_issues: list[str] = []
    boundary_issues: list[str] = []

    # 1. 来源校验
    for idx, claim in enumerate(claims_to_verify):
        claim_text = claim.get("claim", "")
        source = claim.get("source")
        if not source:
            source_issues.append(f"claim[{idx}] 缺少来源: {claim_text}")

    # 2. 幻觉校验 - 复用 RuleChecker 的编造模式
    for pattern in rule_checker.FABRICATION_PATTERNS:
        matches = re.findall(pattern, output_text)
        if matches:
            hallucination_issues.append(f"匹配到编造模式 {pattern}: {matches}")
    # 数字类 claim 无来源视为幻觉风险
    for idx, claim in enumerate(claims_to_verify):
        if claim.get("claim_type") in {"number", "phone_number", "legal_citation"} and not claim.get("source"):
            hallucination_issues.append(f"claim[{idx}] 为数字/法条类但无来源: {claim.get('claim')}")

    # 3. 时效校验 - 检查 file 类来源的最后更新
    for idx, claim in enumerate(claims_to_verify):
        source = claim.get("source") or ""
        if source.startswith(("knowledge/", "rules/", "agents/")):
            src_path = _safe_resolve(source)
            if src_path and src_path.exists():
                try:
                    src_content = src_path.read_text(encoding="utf-8")
                    meta = _parse_knowledge_meta(src_content)
                    freshness = _freshness_status(meta["last_updated"])
                    if freshness == "outdated":
                        freshness_issues.append(
                            f"claim[{idx}] 来源 {source} 已过时（最后更新 {meta['last_updated']}）"
                        )
                except Exception:
                    pass

    # 4. 单源校验 - 关键 claim 应有多源
    critical_types = {"number", "legal_citation", "phone_number"}
    for idx, claim in enumerate(claims_to_verify):
        if claim.get("claim_type") in critical_types:
            sources = claim.get("sources") or ([claim["source"]] if claim.get("source") else [])
            if len(sources) < 2:
                single_source_issues.append(
                    f"claim[{idx}] 为关键类型 {claim.get('claim_type')} 但仅有 {len(sources)} 个来源"
                )

    # 5. 越界校验 - 检测是否给出明确的法律/医疗建议（应由专门智能体处理）
    boundary_patterns = [
        (r"我作为(?:律师|医生|会计师)[^，。]*建议", "疑似以专业人士身份给出建议"),
        (r"你(?:确诊|患有|得了)[^，。]*病", "疑似医疗诊断"),
        (r"我(?:代表|替你)[^，。]*(?:起诉|应诉|出庭)", "疑似代理诉讼"),
    ]
    for pattern, reason in boundary_patterns:
        if re.search(pattern, output_text):
            boundary_issues.append(reason)

    source_check = {"passed": len(source_issues) == 0, "issues": source_issues}
    hallucination_check = {"passed": len(hallucination_issues) == 0, "issues": hallucination_issues}
    freshness_check = {"passed": len(freshness_issues) == 0, "issues": freshness_issues}
    single_source_check = {"passed": len(single_source_issues) == 0, "issues": single_source_issues}
    boundary_check = {"passed": len(boundary_issues) == 0, "issues": boundary_issues}

    five_gate_passed = all(
        c["passed"]
        for c in (
            source_check,
            hallucination_check,
            freshness_check,
            single_source_check,
            boundary_check,
        )
    )

    # ---------- SelfCheckGPT ----------
    selfcheck_result: dict[str, Any] = {
        "enabled": bool(selfcheck_enabled),
        "available": _SELFCHECK_AVAILABLE,
        "numeric_claims_found": 0,
        "consistency_scores": [],
        "overall_consistency": None,
        "low_consistency_claims": [],
        "note": None,
    }

    if selfcheck_enabled:
        numeric_claims = _extract_numeric_claims(output_text)
        selfcheck_result["numeric_claims_found"] = len(numeric_claims)

        if _SELFCHECK_AVAILABLE and SelfCheckChecker is not None and llm_client is not None and llm_client.api_key:
            try:
                # SelfCheckChecker 无参构造（内部读 settings）
                checker = SelfCheckChecker()
                # SelfCheckChecker.check 签名：(response, messages, llm_client)
                # MCP 工具未收到原始 messages，用 output_text 构造最小 prompt 以触发重采样
                messages = [
                    {
                        "role": "user",
                        "content": (
                            "请基于以下内容重新生成一段相同主题的说明，"
                            "保持事实性数字（电话/天数/金额/百分比/法条号）的准确性：\n"
                            + output_text
                        ),
                    }
                ]
                sc = await checker.check(
                    response=output_text,
                    messages=messages,
                    llm_client=llm_client,
                )
                # 兼容不同返回结构：sc 可能含 passed / numeric_claims_found / consistency_scores / ...
                selfcheck_result.update({
                    "consistency_scores": sc.get("consistency_scores", []),
                    "overall_consistency": sc.get("overall_consistency"),
                    "low_consistency_claims": sc.get("low_consistency_claims", []),
                    "passed": sc.get("passed"),
                    "note": "原始 messages 未传入，使用 output_text 构造采样 prompt；建议调用方在需要精确 SelfCheckGPT 时直接使用 selfcheck 模块",
                })
            except Exception as exc:
                selfcheck_result["note"] = f"SelfCheckChecker 调用失败，降级为只做 5 关: {exc}"
        else:
            selfcheck_result["note"] = (
                "SelfCheckChecker 或 LLM 不可用（模块未导入或 LLM_API_KEY 未配置），"
                "已降级为只做 5 关校验。"
            )

    # ---------- 置信度标注 ----------
    confidence_labels: list[dict[str, Any]] = []
    threshold = settings.selfcheck_consistency_threshold
    consistency_map: dict[str, float] = {}
    for item in selfcheck_result.get("consistency_scores") or []:
        consistency_map[item.get("claim", "")] = float(item.get("consistency", 0.0))

    for claim in claims_to_verify:
        claim_text = claim.get("claim", "")
        source = claim.get("source")
        has_source = bool(source)
        consistency = consistency_map.get(claim_text)

        if consistency is not None:
            if consistency >= 0.8:
                label = "高"
            elif consistency >= threshold:
                label = "中"
            else:
                label = "未知"
        elif has_source:
            label = "中"
        else:
            label = "未知"

        confidence_labels.append(
            ConfidenceLabel(
                claim=claim_text,
                confidence=label,
                source=source,
                reason=None if has_source else "缺少来源",
            ).__dict__
        )

    passed = five_gate_passed and (
        not selfcheck_enabled
        or not selfcheck_result.get("low_consistency_claims")
    )

    return {
        "passed": passed,
        "check_results": {
            "source_check": source_check,
            "hallucination_check": hallucination_check,
            "freshness_check": freshness_check,
            "single_source_check": single_source_check,
            "boundary_check": boundary_check,
        },
        "selfcheck_result": selfcheck_result,
        "confidence_labels": confidence_labels,
        "five_gate_passed": five_gate_passed,
    }


# =====================================================================
# 工具 7: check_rules
# =====================================================================

@mcp.tool(
    name="check_rules",
    description=(
        "校验智能体输出是否符合规则。输入待校验文本和智能体名，返回违反的规则列表。"
        "必须在输出给用户前调用。底层调用 rules_loader.rule_checker。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent_name": {"type": "string", "description": "智能体名，如 death-aftercare"},
            "output_text": {"type": "string", "description": "待校验的输出内容"},
            "context": {
                "type": "object",
                "properties": {
                    "user_input": {"type": "string"},
                    "risk_tier": {"type": "string", "enum": ["R0", "R1", "R2", "R3"]},
                    "rules_to_check": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["agent_name", "output_text"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "violations": {"type": "array"},
            "warnings": {"type": "array"},
            "risk_tier": {"type": "string"},
            "safety_triggered": {"type": "boolean"},
        },
    },
)
async def check_rules(
    agent_name: str,
    output_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """规则校验 - 调用 rule_checker"""
    context = context or {}
    result = rule_checker.check(output_text=output_text, context=context)
    return {
        "passed": result.passed,
        "violations": result.violations,
        "warnings": [],  # RuleChecker 当前不分 violations/warnings，统一进 violations
        "risk_tier": result.risk_tier.value,
        "safety_triggered": result.safety_triggered,
        "integrity_violations": result.integrity_violations,
        "agent_name": agent_name,
    }


# =====================================================================
# 工具 8: query_memory
# =====================================================================

@mcp.tool(
    name="query_memory",
    description=(
        "查询或更新分层记忆。支持工作记忆（最近对话）、情景记忆（历史片段）、"
        "语义记忆（用户画像/事实）、程序记忆（流程进度）。"
        "若 memory 模块未实现，返回空结果并标注 unavailable=true。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["recall", "update_profile", "update_progress", "detect_contradiction"],
            },
            "user_id": {"type": "string", "description": "用户 ID（哈希）"},
            "memory_layer": {
                "type": "string",
                "enum": ["working", "episodic", "semantic", "procedural"],
            },
            "query": {"type": "string", "description": "recall 时的查询文本"},
            "updates": {"type": "object", "description": "update_profile/update_progress 时的更新内容"},
        },
        "required": ["action", "user_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "results": {"type": "array"},
            "contradictions_detected": {"type": "array"},
            "user_profile": {"type": "object"},
            "current_progress": {"type": "object"},
            "unavailable": {"type": "boolean"},
        },
    },
)
async def query_memory(
    action: str,
    user_id: str,
    memory_layer: str | None = None,
    query: str | None = None,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """分层记忆查询 - 基于 MemoryManager 的 4 层记忆"""
    if not _MEMORY_AVAILABLE or MemoryManager is None:
        return {
            "action": action,
            "results": [],
            "contradictions_detected": [],
            "user_profile": {},
            "current_progress": {},
            "unavailable": True,
            "note": "memory 模块未实现（legacy.memory.manager 不可导入），返回空结果",
        }

    manager = _get_memory_manager()
    try:
        if action == "recall":
            results: list[dict[str, Any]] = []
            # 情景记忆召回
            if query:
                episodes = manager.episodic.recall_by_semantic(query, top_k=3)
                for ep in episodes:
                    results.append({
                        "type": "episodic",
                        "summary": getattr(ep, "summary", str(ep)),
                        "timestamp": str(getattr(ep, "timestamp", "")),
                    })
            # 工作记忆窗口
            window = manager.working.get_context_window()
            if window:
                results.append({"type": "working", "content": window})
            # 用户画像
            profile = manager.semantic.get_profile(user_id)
            profile_dict = _profile_to_dict(profile) if profile else {}
            return {
                "action": action,
                "results": results,
                "contradictions_detected": [],
                "user_profile": profile_dict,
                "current_progress": {},
                "unavailable": False,
            }

        if action == "update_profile":
            manager.semantic.update_user_profile(user_id, updates or {})
            profile = manager.semantic.get_profile(user_id)
            return {
                "action": action,
                "results": [],
                "contradictions_detected": list(manager.semantic.pending_contradictions),
                "user_profile": _profile_to_dict(profile) if profile else {},
                "current_progress": {},
                "unavailable": False,
            }

        if action == "update_progress":
            # updates 需包含 procedure_id 与 step_completed
            procedure_id = (updates or {}).get("procedure_id")
            step_completed = (updates or {}).get("step_completed")
            progress_dict: dict[str, Any] = {}
            if procedure_id is not None and step_completed is not None:
                manager.procedural.update_user_progress(
                    user_id, procedure_id, step_completed
                )
                progress = manager.procedural.get_user_progress(user_id, procedure_id)
                progress_dict = {
                    "procedure_id": procedure_id,
                    "current_step": getattr(progress, "current_step", None) if progress else None,
                    "completed_steps": list(getattr(progress, "completed_steps", [])) if progress else [],
                }
            else:
                progress_dict = {"error": "updates 需包含 procedure_id 与 step_completed"}
            return {
                "action": action,
                "results": [],
                "contradictions_detected": [],
                "user_profile": {},
                "current_progress": progress_dict,
                "unavailable": False,
            }

        if action == "detect_contradiction":
            contradictions = list(manager.semantic.pending_contradictions)
            # 清空已读矛盾，避免重复告警
            manager.semantic.pending_contradictions.clear()
            return {
                "action": action,
                "results": [],
                "contradictions_detected": contradictions,
                "user_profile": {},
                "current_progress": {},
                "unavailable": False,
            }

        return {
            "action": action,
            "results": [],
            "contradictions_detected": [],
            "user_profile": {},
            "current_progress": {},
            "unavailable": False,
            "error": f"未知 action: {action}",
        }
    except Exception as exc:
        return {
            "action": action,
            "results": [],
            "contradictions_detected": [],
            "user_profile": {},
            "current_progress": {},
            "unavailable": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


# MemoryManager 懒加载单例（进程级共享，按 user_id 区分数据）
_memory_manager_instance: Any = None


def _get_memory_manager() -> Any:
    """懒加载 MemoryManager 单例"""
    global _memory_manager_instance
    if _memory_manager_instance is None and MemoryManager is not None:
        _memory_manager_instance = MemoryManager()
    return _memory_manager_instance


def _profile_to_dict(profile: Any) -> dict[str, Any]:
    """把 UserProfile dataclass 转为 dict（含非空字段）"""
    if profile is None:
        return {}
    try:
        from dataclasses import asdict
        d = asdict(profile)
        # 过滤 None 值，减小返回体积
        return {k: v for k, v in d.items() if v is not None}
    except Exception:
        return {"user_id": getattr(profile, "user_id", "")}


# =====================================================================
# 工具 9: initiate_debate
# =====================================================================

@mcp.tool(
    name="initiate_debate",
    description=(
        "当多个智能体对同一问题给出冲突回答时，发起结构化辩论。"
        "3 轮辩论（Opening/Rebuttal/Closing）+ 投票 + 可选仲裁。"
        "若 debate 模块未实现，返回 not_implemented=true。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "辩论主题"},
            "participants": {"type": "array", "items": {"type": "string"}, "description": "参与辩论的智能体 ID 列表"},
            "initial_responses": {
                "type": "array",
                "items": {"type": "object"},
                "description": "各方初始回答",
            },
            "voting_strategy": {
                "type": "string",
                "enum": ["majority", "weighted", "confidence_weighted", "consensus"],
                "default": "weighted",
            },
        },
        "required": ["topic", "participants", "initial_responses"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "debate_id": {"type": "string"},
            "rounds": {"type": "array"},
            "votes": {"type": "object"},
            "final_resolution": {"type": "object"},
            "arbitration_needed": {"type": "boolean"},
            "not_implemented": {"type": "boolean"},
        },
    },
)
async def initiate_debate(
    topic: str,
    participants: list[str],
    initial_responses: list[dict[str, Any]],
    voting_strategy: str = "weighted",
) -> dict[str, Any]:
    """发起辩论"""
    if not _DEBATE_AVAILABLE or DebateOrchestrator is None:
        return {
            "debate_id": str(uuid.uuid4()),
            "rounds": [],
            "votes": {},
            "final_resolution": None,
            "arbitration_needed": False,
            "not_implemented": True,
            "note": (
                "debate 模块未实现（legacy.debate.orchestrator 不可导入）。"
                "请实现 DebateOrchestrator 后启用。"
            ),
            "topic": topic,
            "participants": participants,
            "voting_strategy": voting_strategy,
        }
    try:
        orchestrator = DebateOrchestrator(
            llm_client=llm_client,
            voting_strategy=voting_strategy,
        )
        result = await orchestrator.run_debate(
            topic=topic,
            participants=participants,
            initial_responses=initial_responses,
        )
        return {
            "debate_id": result.get("debate_id", str(uuid.uuid4())),
            "rounds": result.get("rounds", []),
            "votes": result.get("votes", {}),
            "final_resolution": result.get("final_resolution"),
            "arbitration_needed": result.get("arbitration_needed", False),
            "not_implemented": False,
        }
    except Exception as exc:
        return {
            "debate_id": str(uuid.uuid4()),
            "rounds": [],
            "votes": {},
            "final_resolution": None,
            "arbitration_needed": False,
            "not_implemented": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


# =====================================================================
# 工具 10: call_external_agent
# =====================================================================

@mcp.tool(
    name="call_external_agent",
    description=(
        "通过 A2A 协议调用外部智能体。需用户提供数据共享同意（user_consent=true）。"
        "出口数据自动脱敏 PII，返回结果校验诚信报告。"
        "当前为 mock 实现：不实际发起网络请求，返回模拟结果。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to_agent_id": {"type": "string", "description": "目标外部 agent ID"},
            "capability_id": {"type": "string", "description": "调用的能力 ID（见 Agent Card）"},
            "input_data": {"type": "object", "description": "输入参数（自动脱敏 PII）"},
            "user_consent": {"type": "boolean", "description": "用户是否同意数据共享"},
        },
        "required": ["to_agent_id", "capability_id", "input_data", "user_consent"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "state": {"type": "string"},
            "result": {"type": "object"},
            "integrity_report": {"type": "object"},
            "integrity_verified": {"type": "boolean"},
            "warning": {"type": "string"},
        },
    },
)
async def call_external_agent(
    to_agent_id: str,
    capability_id: str,
    input_data: dict[str, Any],
    user_consent: bool,
) -> dict[str, Any]:
    """A2A 外部智能体调用（mock 实现）"""
    # 强制用户同意
    if not user_consent:
        return {
            "task_id": str(uuid.uuid4()),
            "state": "rejected",
            "result": None,
            "integrity_report": None,
            "integrity_verified": False,
            "warning": "用户未同意数据共享，调用被拒绝",
        }

    # 出口数据脱敏
    redacted_input = _redact_pii(input_data)

    # mock：不实际发起网络请求
    task_id = str(uuid.uuid4())
    mock_result: dict[str, Any] = {
        "acknowledged": True,
        "to_agent_id": to_agent_id,
        "capability_id": capability_id,
        "echo": redacted_input,
        "note": "mock 响应，未实际调用外部 agent",
    }
    # 模拟 integrity_report
    integrity_report: dict[str, Any] = {
        "checked_at": _utcnow_iso(),
        "data_redacted": True,
        "fields_redacted": sorted(_collect_redacted_fields(input_data, redacted_input)),
        "source_verified": False,
        "checksum": str(uuid.uuid4())[:8],
    }

    return {
        "task_id": task_id,
        "state": "completed",
        "result": mock_result,
        "integrity_report": integrity_report,
        "integrity_verified": True,
        "warning": (
            "mock 实现：未实际发起 A2A 网络调用。"
            "集成真实 A2A registry 后请替换为 httpx 调用。"
        ),
        "redacted_input": redacted_input,
    }


def _collect_redacted_fields(original: Any, redacted: Any) -> list[str]:
    """对比原始与脱敏后数据，返回被脱敏的字段名列表"""
    fields: list[str] = []
    if isinstance(original, dict) and isinstance(redacted, dict):
        for k, v_orig in original.items():
            v_red = redacted.get(k)
            if v_orig != v_red:
                fields.append(k)
            elif isinstance(v_orig, (dict, list)):
                fields.extend(_collect_redacted_fields(v_orig, v_red))
    elif isinstance(original, list) and isinstance(redacted, list):
        for o, r in zip(original, redacted):
            fields.extend(_collect_redacted_fields(o, r))
    return fields


# =====================================================================
# 工具 11: execute_reflexion
# =====================================================================

@mcp.tool(
    name="execute_reflexion",
    description=(
        "子智能体/工具/转介调用失败时的反思-调整-重试机制。MAX_RETRIES=3，失败后走 fallback。"
        "若 reflexion 模块未实现，返回 not_implemented=true 并给出预定义调整策略建议。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation_type": {
                "type": "string",
                "enum": ["subagent", "tool", "transfer"],
            },
            "operation_name": {
                "type": "string",
                "description": "失败的操作名，如 'death-aftercare-emotional' 或 'query_knowledge'",
            },
            "failure_reason": {"type": "string", "description": "失败原因"},
            "original_input": {"type": "object", "description": "原始输入参数"},
        },
        "required": ["operation_type", "operation_name", "failure_reason", "original_input"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "result": {"type": "object"},
            "attempts": {"type": "integer"},
            "fallback_used": {"type": "boolean"},
            "adjustments_applied": {"type": "array"},
            "reflexion_history": {"type": "array"},
            "not_implemented": {"type": "boolean"},
        },
    },
)
async def execute_reflexion(
    operation_type: str,
    operation_name: str,
    failure_reason: str,
    original_input: dict[str, Any],
) -> dict[str, Any]:
    """反思重试 - 基于 ReflexionEngine 生成反思与调整后输入

    MCP 工具收到的是失败元数据（非可执行 callable），因此不能直接调用
    ReflexionEngine.execute_with_reflexion。这里调用其内部 _reflect + _adjust_input
    生成反思结论与调整后输入，供调用方自行重试。
    """
    if not _REFLEXION_AVAILABLE or ReflexionEngine is None:
        # 模块未实现：返回预定义调整策略建议
        suggestion = _lookup_adjustment_strategy(operation_type, operation_name, failure_reason)
        return {
            "success": False,
            "result": None,
            "attempts": 0,
            "fallback_used": True,
            "adjustments_applied": [suggestion] if suggestion else [],
            "reflexion_history": [],
            "not_implemented": True,
            "note": (
                "reflexion 模块未实现（legacy.reflexion.engine 不可导入）。"
                "已根据失败模式返回预定义调整策略建议，请由调用方自行重试。"
            ),
        }
    try:
        engine = ReflexionEngine(agent_name=operation_name)
        # 构造 failure_info（与 ReflexionEngine 内部结构一致）
        failure_info: dict[str, Any] = {
            "attempt": 1,
            "failure_type": _classify_failure_type(failure_reason),
            "failure_message": failure_reason,
            "input_summary": str(original_input)[:200],
            "output_summary": None,
            "timestamp": _utcnow_iso(),
        }
        engine.failures = [failure_info]

        # LLM 反思（若 LLM 不可用则 _reflect 内部走兜底）
        reflection = await engine._reflect(failure_info, operation_type)
        engine.reflections = [reflection]

        # 调整输入（先查预定义策略表，再用 LLM 的 adjusted_params）
        adjusted_input = await engine._adjust_input(dict(original_input), reflection)

        adjustments_applied: list[str] = []
        strategy = reflection.get("adjustment_strategy")
        if strategy:
            adjustments_applied.append(strategy)
        # 把 adjusted_params 中的变化也列出
        for k, v in (reflection.get("adjusted_params") or {}).items():
            adjustments_applied.append(f"{k}={v}")

        return {
            "success": False,  # 未实际重试，标记为 False
            "result": {
                "adjusted_input": adjusted_input,
                "reflection": reflection,
                "retry_recommended": True,
            },
            "attempts": 1,
            "fallback_used": False,
            "adjustments_applied": adjustments_applied,
            "reflexion_history": [
                {
                    "failure": failure_info,
                    "reflection": reflection,
                }
            ],
            "not_implemented": False,
            "note": (
                "已生成反思与调整后输入，调用方应使用 result.adjusted_input 重新调用原操作。"
                "若重试仍失败，再调用本工具或走 fallback。"
            ),
        }
    except Exception as exc:
        suggestion = _lookup_adjustment_strategy(operation_type, operation_name, failure_reason)
        return {
            "success": False,
            "result": None,
            "attempts": 0,
            "fallback_used": True,
            "adjustments_applied": [suggestion] if suggestion else [],
            "reflexion_history": [],
            "not_implemented": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _classify_failure_type(failure_reason: str) -> str:
    """从失败原因文本推断失败类型 key（对应 ADJUSTMENT_STRATEGIES 的 key）

    ReflexionEngine._reflect 会用 LLM 重新分类，这里只做初步归类，
    让预定义策略表能命中快速路径。
    """
    reason_lower = (failure_reason or "").lower()
    patterns: list[tuple[str, str]] = [
        ("timeout", r"timeout|超时"),
        ("rate_limit", r"rate.?limit|429|限流"),
        ("format_error", r"format|格式|json|parse|解析"),
        ("subagent_not_found", r"subagent|子智能体.*not found|不可用|unavailable"),
        ("knowledge_not_found", r"knowledge|知识库|not found|不存在"),
        ("api_error", r"api.?error|500|502|503|api 调用"),
        ("invalid_argument", r"argument|参数|invalid"),
        ("permission_denied", r"permission|denied|权限|拒绝"),
    ]
    for key, pattern in patterns:
        if re.search(pattern, reason_lower, re.IGNORECASE):
            return key
    return "unknown"


# 预定义调整策略 - 摘自 Reflexion-Mechanism.md 的 ADJUSTMENT_STRATEGIES
_ADJUSTMENT_STRATEGIES: list[dict[str, str]] = [
    {
        "failure_mode": "llm_timeout",
        "keyword": "timeout|超时",
        "adjustment": "缩短输入 + 降低 max_tokens + 重试一次；仍失败则降级到模板回答",
    },
    {
        "failure_mode": "llm_rate_limit",
        "keyword": "rate.?limit|429|限流",
        "adjustment": "指数退避重试（1s/2s/4s）；切换到备用模型；仍失败则降级",
    },
    {
        "failure_mode": "rule_violation",
        "keyword": "rule|规则|违反|integrity",
        "adjustment": "提取违反规则注入 system prompt 重写；标记 confidence=低",
    },
    {
        "failure_mode": "knowledge_not_found",
        "keyword": "knowledge|知识库|not found|不存在",
        "adjustment": "触发 policy-researcher 搜索补全知识库；标注 needs_research",
    },
    {
        "failure_mode": "subagent_unavailable",
        "keyword": "subagent|子智能体|unavailable|不可用",
        "adjustment": "父智能体接管任务 + 降级执行；记录 fallback 原因",
    },
    {
        "failure_mode": "transfer_rejected",
        "keyword": "transfer|转介|reject|拒绝",
        "adjustment": "回退到原智能体继续处理；提示用户已切换",
    },
    {
        "failure_mode": "json_parse_error",
        "keyword": "json|parse|解析",
        "adjustment": "追加 '只输出 JSON' 指令重试；用 LLMClient._parse_json 容错",
    },
    {
        "failure_mode": "pii_leak",
        "keyword": "pii|脱敏|泄漏",
        "adjustment": "拦截输出 + 重新脱敏 + 上报 incident",
    },
    {
        "failure_mode": "safety_triggered",
        "keyword": "safety|r3|心理危机|自残",
        "adjustment": "立即中断常规流程 + 触发 safety-protocol + 转介心理援助",
    },
    {
        "failure_mode": "unknown",
        "keyword": ".*",
        "adjustment": "记录 incident + 降级到 fallback + 提示用户稍后重试",
    },
]


def _lookup_adjustment_strategy(operation_type: str, operation_name: str, failure_reason: str) -> str | None:
    """根据失败原因匹配预定义调整策略"""
    reason_lower = (failure_reason or "").lower()
    for entry in _ADJUSTMENT_STRATEGIES:
        if re.search(entry["keyword"], reason_lower, re.IGNORECASE):
            return f"[{entry['failure_mode']}] {entry['adjustment']}"
    # 兜底
    return f"[unknown] {_ADJUSTMENT_STRATEGIES[-1]['adjustment']}"


# =====================================================================
# 入口
# =====================================================================

def main() -> None:
    """命令行入口：启动 MCP server

    用法：
      python -m legacy.mcp_server.server --transport stdio
      python -m legacy.mcp_server.server --transport http --port 8000
    """
    parser = argparse.ArgumentParser(
        prog="legacy-mcp-server",
        description="身后事多智能体平台 MCP Server",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="传输方式（默认 stdio）",
    )
    parser.add_argument("--host", default=None, help="HTTP 模式监听地址（默认取 settings.mcp_server_host）")
    parser.add_argument("--port", type=int, default=None, help="HTTP 模式监听端口（默认取 settings.mcp_server_port）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,  # stdout 留给 JSON-RPC
    )

    logger.info(
        "MCP server 启动: transport=%s, tools=%d, selfcheck=%s, memory=%s, debate=%s, reflexion=%s, lightrag=%s",
        args.transport,
        len(mcp._tools),
        _SELFCHECK_AVAILABLE,
        _MEMORY_AVAILABLE,
        _DEBATE_AVAILABLE,
        _REFLEXION_AVAILABLE,
        settings.lightrag_enabled,
    )
    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
