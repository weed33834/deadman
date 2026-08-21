"""Web 聊天与身份/CLI 服务的原生实现（从废弃的 web/server.py 抽出）。

历史问题：app.py 直接 import 已标记 DEPRECATED 的 ``web.server.web_server`` 单例，
并用 ``_WfileAdapter`` hack 把旧同步 ``_stream_chat``（写 wfile）接到 FastAPI SSE。
此处把这些核心逻辑原生化为服务层：

- ``handle_chat``：走 orchestration graph 完整规则链的对话（原 ``_handle_chat``）。
- ``stream_chat_events``：**异步生成器**，产出 SSE 行字符串（原 ``_stream_chat`` 写
  wfile 改为 yield），app.py 直接 ``async for`` 消费，无需再包 wfile 适配器。
- ``whoami`` / ``cli``：平台身份告知与白名单 CLI 代理。
- ``split_for_streaming``：响应按句子切块。
- 对话级统计：模块级状态，``record_conversation_stats`` 累加、``get_conversation_stats``
  快照读取。

``web.server.WebServer`` 现仅作薄委托壳（保留旧方法签名，旧测试继续通过）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, AsyncIterator

from ..._version import __version__ as DEADMAN_VERSION
from ...config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# CLI 白名单（原 web/server.py 迁移）
# =====================================================================
_CLI_COMMANDS: frozenset[str] = frozenset(
    {
        "version", "eval-list",
        "llm-test", "llm-sync-models", "llm-cost",
        "prompt-list", "prompt-sync",
        "rule-test", "rule-validate",
        "agent-list", "agent-ping",
        "knowledge-list", "knowledge-freshness",
        "tool-list", "mcp-ping",
        "obs-dashboard", "obs-test", "obs-export",
        "memory-list", "memory-test", "memory-ping",
        "a2a-card", "a2a-test", "a2a-registry",
        "deploy-check", "deploy-test",
        "reflexion-list", "reflexion-test", "reflexion-ping",
        "skill-list", "skill-validate",
        "alignment-status", "alignment-train",
        "governance-status", "governance-check",
        "multimodal-status", "multimodal-test",
    }
)

# =====================================================================
# 对话级统计（模块级状态，替代 WebServer._conversation_stats）
# =====================================================================
_stats_lock = threading.Lock()
_conversation_stats: dict[str, Any] = {
    "agent_calls": {},
    "risk_tier_counts": {},
    "span_type_counts": {},
    "token_usage_total": {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    },
    "termination_triggers": {},
    "total_conversations": 0,
    "degraded_count": 0,
    "recent_spans": [],
}


def record_conversation_stats(
    *,
    agent: str | None,
    risk_tier: str,
    trace_spans: list[dict[str, Any]] | None,
    subagent_called: list[str] | None,
    metrics: dict[str, Any] | None,
    degraded: bool,
    forced_terminate: bool = False,
) -> None:
    """累加对话级统计（best-effort，失败不阻塞对话）。"""
    try:
        with _stats_lock:
            stats = _conversation_stats
            agent_key = agent or "unknown"
            stats["agent_calls"][agent_key] = stats["agent_calls"].get(agent_key, 0) + 1
            tier = risk_tier or "R0"
            stats["risk_tier_counts"][tier] = stats["risk_tier_counts"].get(tier, 0) + 1
            for span in trace_spans or []:
                if isinstance(span, dict) and span.get("span_type"):
                    st = span["span_type"]
                    stats["span_type_counts"][st] = stats["span_type_counts"].get(st, 0) + 1
            tu = (metrics or {}).get("token_usage") or {}
            if isinstance(tu, dict):
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    stats["token_usage_total"][k] = (
                        stats["token_usage_total"].get(k, 0) + int(tu.get(k, 0) or 0)
                    )
            if forced_terminate:
                source = "forced_terminate"
                for span in reversed(trace_spans or []):
                    if not isinstance(span, dict):
                        continue
                    attrs = span.get("attributes") or {}
                    if not isinstance(attrs, dict):
                        continue
                    if attrs.get("termination_source"):
                        source = str(attrs["termination_source"])
                        break
                    if attrs.get("termination"):
                        source = str(attrs["termination"])
                        break
                stats["termination_triggers"][source] = (
                    stats["termination_triggers"].get(source, 0) + 1
                )
            stats["total_conversations"] += 1
            if degraded:
                stats["degraded_count"] += 1
            stats["recent_spans"].append(
                {
                    "timestamp": time.time(),
                    "agent": agent or "unknown",
                    "degraded": degraded,
                }
            )
            stats["recent_spans"] = stats["recent_spans"][-20:]
    except Exception as exc:  # pragma: no cover - 统计失败不阻断
        logger.debug("record_conversation_stats 失败: %s", exc)


def get_conversation_stats() -> dict[str, Any]:
    """返回对话级统计的深拷贝快照。"""
    with _stats_lock:
        return copy.deepcopy(_conversation_stats)


def reset_conversation_stats() -> None:
    """重置统计（测试隔离）。"""
    global _conversation_stats
    with _stats_lock:
        _conversation_stats = {
            "agent_calls": {},
            "risk_tier_counts": {},
            "span_type_counts": {},
            "token_usage_total": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "termination_triggers": {},
            "total_conversations": 0,
            "degraded_count": 0,
            "recent_spans": [],
        }


# =====================================================================
# 对话
# =====================================================================


def _normalize_agent(agent: str | None) -> str:
    """智能体名：前端短横线 → graph 内部下划线。"""
    return (agent or "death-aftercare").replace("-", "_")


def _session_id(user_id: str | None, prefix: str) -> str:
    return f"{prefix}{user_id or 'anon'}-{int(time.time())}"


def _parse_rule_check(rule_check: Any) -> tuple[str, bool, list[str]]:
    """从 rule_check 抽取 risk_tier / safety_triggered / violations。"""
    if rule_check is not None:
        risk_tier = getattr(getattr(rule_check, "risk_tier", None), "value", "R0")
        safety_triggered = bool(getattr(rule_check, "safety_triggered", False))
        violations = list(getattr(rule_check, "violations", None) or [])
        return str(risk_tier), safety_triggered, violations
    return "R0", False, []


async def handle_chat(
    agent: str,
    query: str,
    history: list,
    user_id: str | None = None,
) -> dict[str, Any]:
    """处理对话 - 走 orchestration/graph 完整规则链（原 WebServer._handle_chat）。

    graph 失败时降级到 llm_client（用 SoulLoader.default_soul() 作最低身份约束）。
    """
    if not query:
        return {"error": "query 不能为空"}

    from ...memory.manager import MemoryManager
    from ...orchestration.graph import build_main_graph
    from ...orchestration.state import ConversationState

    session_id = _session_id(user_id, "web-")
    agent_normalized = _normalize_agent(agent)
    state = ConversationState(
        user_input=query,
        current_agent=agent_normalized,
        session_id=session_id,
        agent_name=agent_normalized,  # type: ignore[typeddict-unknown-key]
        user_id=user_id or "anonymous",  # type: ignore[typeddict-unknown-key]
        history=list(history[-10:]),  # type: ignore[typeddict-unknown-key]
    )

    try:
        graph = build_main_graph()
        thread_id = state.get("session_id") or state.get("user_id") or "default"
        result_state = await graph.ainvoke(
            state, config={"configurable": {"thread_id": thread_id}}
        )
        response = result_state.get("final_response") or result_state.get("draft_response", "")
        actual_agent = (result_state.get("current_agent") or agent_normalized).replace("_", "-")
        risk_tier, safety_triggered, rule_violations = _parse_rule_check(
            result_state.get("rule_check")
        )
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
            logger.warning("MemoryManager.after_turn 失败（不影响响应）: %s", exc)
        record_conversation_stats(
            agent=actual_agent,
            risk_tier=risk_tier,
            trace_spans=list(result_state.get("trace_spans") or []),
            subagent_called=list(result_state.get("subagent_called") or []),
            metrics=dict(result_state.get("metrics") or {}),
            degraded=False,
            forced_terminate=bool(result_state.get("forced_terminate")),
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
        from ...llm import llm_client
        from ...soul_loader import SoulLoader

        if not llm_client.api_key:
            return {
                "response": "服务暂不可用（LLM 未配置）。",
                "agent": agent,
                "degraded": True,
                "error": "llm_not_configured",
            }
        messages: list[dict[str, str]] = (
            [{"role": "system", "content": SoulLoader().default_soul()}]
            + [
                {"role": item.get("role", "user"), "content": item.get("content", "")}
                for item in history[-10:]
                if isinstance(item, dict)
            ]
            + [{"role": "user", "content": query}]
        )
        try:
            response = await llm_client.chat(messages, temperature=0.3)
        except Exception as fallback_exc:
            return {
                "response": f"服务暂不可用: {fallback_exc}",
                "agent": agent,
                "degraded": True,
                "error": str(fallback_exc),
            }
        record_conversation_stats(
            agent=agent,
            risk_tier="R0",
            trace_spans=[],
            subagent_called=[],
            metrics={},
            degraded=True,
            forced_terminate=False,
        )
        return {
            "response": response,
            "agent": agent,
            "degraded": True,
            "error": str(exc),
            "degraded_reason": "graph_failed_using_fallback",
        }


async def stream_chat_events(
    agent: str,
    query: str,
    user_id: str | None = None,
) -> AsyncIterator[str]:
    """流式对话：异步生成器，逐条产出 SSE 行字符串（原 _stream_chat 写 wfile 改为 yield）。

    事件类型：
        - ``data: {...}``            内容分块
        - ``event: trace``           思考过程 / 工具调用
        - ``event: done``            结束（含 safety_triggered）
        - ``event: error``           错误
    """
    if not query:
        yield "event: error\ndata: " + json.dumps({"error": "query 不能为空"}) + "\n\n"
        return

    from ...llm import llm_client
    from ...memory.manager import MemoryManager
    from ...orchestration.graph import build_main_graph
    from ...orchestration.state import ConversationState
    from ...soul_loader import SoulLoader

    agent_normalized = _normalize_agent(agent)
    session_id = _session_id(user_id, "web-stream-")
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
    trace_spans: list[dict[str, Any]] = []
    trace_metrics: dict[str, Any] = {}
    subagent_called: list[str] = []
    draft_response = ""

    # 走 graph（与 handle_chat 一致的规则链）
    try:
        graph = build_main_graph()
        thread_id = state.get("session_id") or state.get("user_id") or "default"
        result_state = await graph.ainvoke(
            state, config={"configurable": {"thread_id": thread_id}}
        )
        response_text = result_state.get("final_response") or result_state.get(
            "draft_response", ""
        )
        draft_response = result_state.get("draft_response", "") or ""
        risk_tier, safety_triggered, _ = _parse_rule_check(result_state.get("rule_check"))
        trace_spans = list(result_state.get("trace_spans") or [])
        trace_metrics = dict(result_state.get("metrics") or {})
        subagent_called = list(result_state.get("subagent_called") or [])
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
        record_conversation_stats(
            agent=agent_normalized.replace("_", "-"),
            risk_tier=risk_tier,
            trace_spans=trace_spans,
            subagent_called=subagent_called,
            metrics=trace_metrics,
            degraded=False,
            forced_terminate=bool(result_state.get("forced_terminate")),
        )
    except Exception:
        logger.exception("stream graph 调用失败，降级到 SoulLoader")
        degraded = True
        if not llm_client.api_key:
            yield "event: error\ndata: " + json.dumps({"error": "LLM API key 未配置"}) + "\n\n"
            return
        messages = [
            {"role": "system", "content": SoulLoader().default_soul()},
            {"role": "user", "content": query},
        ]
        try:
            response_text = await llm_client.chat(messages, temperature=0.3)
        except Exception as fallback_exc:
            err = json.dumps({"error": f"服务暂不可用: {fallback_exc}"}, ensure_ascii=False)
            yield f"event: error\ndata: {err}\n\n"
            return
        record_conversation_stats(
            agent=agent_normalized.replace("_", "-"),
            risk_tier="R0",
            trace_spans=[],
            subagent_called=[],
            metrics={},
            degraded=True,
            forced_terminate=False,
        )

    # 内容分块推送
    for chunk in split_for_streaming(response_text):
        data = json.dumps(
            {"chunk": chunk, "degraded": degraded, "risk_tier": risk_tier},
            ensure_ascii=False,
        )
        yield f"data: {data}\n\n"

    # trace 事件
    if trace_spans or subagent_called or trace_metrics:
        trace_payload = {
            "spans": trace_spans,
            "metrics": trace_metrics,
            "subagent_called": subagent_called,
            "draft_response": draft_response,
            "agent": agent_normalized.replace("_", "-"),
            "degraded": degraded,
        }
        trace_data = json.dumps(trace_payload, ensure_ascii=False)
        yield f"event: trace\ndata: {trace_data}\n\n"

    # 结束事件
    done_data = json.dumps(
        {
            "degraded": degraded,
            "risk_tier": risk_tier,
            "safety_triggered": safety_triggered,
            "agent": agent_normalized.replace("_", "-"),
            "has_trace": bool(trace_spans or subagent_called),
        },
        ensure_ascii=False,
    )
    yield f"event: done\ndata: {done_data}\n\n"


def split_for_streaming(text: str) -> list[str]:
    """把完整响应切成适合 SSE 流式推送的小块（保留分隔符在块尾）。"""
    if not text:
        return [""]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for ch in text:
        buf.append(ch)
        buf_len += 1
        if ch in "\n。！？!?；;" and buf_len >= 4:
            chunks.append("".join(buf))
            buf, buf_len = [], 0
        elif ch == "," and buf_len >= 12:
            chunks.append("".join(buf))
            buf, buf_len = [], 0
        elif buf_len >= 120:
            chunks.append("".join(buf))
            buf, buf_len = [], 0
    if buf:
        chunks.append("".join(buf))
    return chunks


# =====================================================================
# 身份与 CLI
# =====================================================================


def whoami() -> dict[str, Any]:
    """平台身份告知（transparency-framework L5 强制）。"""
    return {
        "platform": "deadman",
        "version": DEADMAN_VERSION,
        "is_ai": True,
        "disclaimer": (
            "本平台是信息引导工具，不代办、不代查、不出具法律意见、不与殡葬机构分成。"
        ),
        "rules_count": 15,
        "agents": [
            "death-aftercare",
            "legal-advisor",
            "financial-analyst",
            "policy-researcher",
            "cross-border-specialist",
            "medical-guide",
            "deep-researcher",
            "data-analyst",
        ],
        "supported_languages": ["zh-CN", "en-US"],
    }


def cli(command: str, req: dict[str, Any]) -> dict[str, Any]:
    """通用 CLI 代理 - subprocess 调用 deadman.cli <command>（白名单）。"""
    if command not in _CLI_COMMANDS:
        return {"ok": False, "error": f"不允许的命令: {command}", "allowed": sorted(_CLI_COMMANDS)}

    cmd_args = [sys.executable, "-m", "deadman.cli", command]
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
        return {"ok": False, "error": f"命令超时（{timeout}s）", "command": command, "returncode": -1}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "command": command, "returncode": -1}


# =====================================================================
# Dead Man Switch auto-ticker（原 web/server.py 模块级函数迁移）
# =====================================================================
# 保存运行中的 ticker/loop 引用，供 stop_switch_auto_ticker 优雅停止
_ticker_state: dict[str, Any] = {}


def maybe_start_switch_auto_ticker() -> threading.Thread | None:
    """按环境变量决定是否启动 SwitchAutoTicker 后台线程（daemon 线程内独立事件循环）。"""
    enabled = os.getenv("DEADMAN_SWITCH_AUTO_TICK_ENABLED", "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        logger.info("SwitchAutoTicker 已通过环境变量禁用")
        return None
    try:
        interval = int(os.getenv("DEADMAN_SWITCH_AUTO_TICK_INTERVAL", "300"))
    except ValueError:
        interval = 300
    if interval <= 0:
        interval = 300

    def _run_loop() -> None:
        import asyncio as _asyncio

        from ...deadman_switch.auto_tick import SwitchAutoTicker
        from ...deadman_switch.store import SwitchStore

        try:
            store = SwitchStore()
            ticker = SwitchAutoTicker(store)
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            _ticker_state["ticker"] = ticker
            _ticker_state["loop"] = loop
            try:
                loop.run_until_complete(ticker.run_forever(interval_seconds=interval))
            finally:
                loop.close()
                _ticker_state.clear()
        except Exception as exc:  # pragma: no cover - 后台调度器异常不阻断主流程
            logger.exception("SwitchAutoTicker 后台线程异常退出: %s", exc)

    thread = threading.Thread(
        target=_run_loop,
        name="deadman-switch-auto-ticker",
        daemon=True,
    )
    thread.start()
    logger.info("SwitchAutoTicker 后台线程已启动 interval=%ss", interval)
    return thread


def stop_switch_auto_ticker(thread: threading.Thread | None) -> None:
    """服务器退出时停止后台调度器。"""
    if thread is None:
        return
    ticker = _ticker_state.get("ticker")
    loop = _ticker_state.get("loop")
    if ticker is not None:
        try:
            ticker.stop()
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("ticker.stop() 异常: %s", exc)
    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("loop.stop() 异常: %s", exc)
    if thread.is_alive():
        thread.join(timeout=2.0)
        if thread.is_alive():  # pragma: no cover - 极端情况
            logger.warning("SwitchAutoTicker 线程 2s 内未退出")
