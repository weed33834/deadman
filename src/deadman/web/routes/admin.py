"""G1 管理台后端 —— /api/admin/*（只读监控 + 模型切换 + 工具启停）

对照 agent-builder-skill 完整版补齐管理台能力（overview/models/tools/prompts/
orchestration/agents/graph/monitoring/evaluation/memory/config）。
资源级增删改调见 resources.py。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

_ADMIN_RUNTIME = Path.home() / ".deadman" / "admin_runtime.json"

_MAIN_AGENTS = [
    {"id": "death-aftercare", "name": "身后事流程引导员"},
    {"id": "legal-advisor", "name": "法律咨询智能体"},
    {"id": "financial-analyst", "name": "财务分析智能体"},
    {"id": "policy-researcher", "name": "政策研究智能体"},
    {"id": "cross-border-specialist", "name": "跨境事务智能体"},
    {"id": "medical-guide", "name": "医疗导航智能体"},
]


def _load_runtime() -> dict[str, Any]:
    if _ADMIN_RUNTIME.exists():
        try:
            return json.loads(_ADMIN_RUNTIME.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_runtime(data: dict[str, Any]) -> None:
    _ADMIN_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    _ADMIN_RUNTIME.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:
        logger.debug("admin 数据源失败: %s", exc)
        return default


def _llm_snapshot() -> dict[str, Any]:
    from ...llm import llm_client

    return {
        "provider": llm_client.provider,
        "model": llm_client.model,
        "api_key_set": bool(llm_client.api_key),
        "base_url": llm_client.base_url,
    }


def _provider_catalog() -> dict[str, Any]:
    from ...llm import PROVIDER_MODELS

    return {provider: {"models": models} for provider, models in PROVIDER_MODELS.items()}


def _tools_snapshot() -> dict[str, Any]:
    from ...mcp_server.server import list_tool_states, mcp

    states = list_tool_states()
    tools = []
    for t in mcp.list_tools():
        tools.append(
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "enabled": states.get(t["name"], True),
                "input_schema": t.get("inputSchema", {}),
            }
        )
    return {"tools": tools, "total": len(tools), "enabled": sum(1 for t in tools if t["enabled"])}


def _external_mcp_snapshot() -> list[dict[str, Any]]:
    from ...mcp_server.client import get_client_manager

    return get_client_manager().list_servers()


def _agent_graph() -> list[dict[str, Any]]:
    agents_dir = None
    try:
        from ...config import settings

        agents_dir = settings.agents_dir
    except Exception:
        pass

    def _sub_agents(main_id: str) -> list[dict[str, Any]]:
        subs: list[dict[str, Any]] = []
        if agents_dir and agents_dir.exists():
            prefix = f"{main_id}-"
            for md in sorted(agents_dir.glob("*.md")):
                name = md.stem
                if name.startswith(prefix):
                    subs.append({"id": name, "name": name.replace(main_id + "-", "").title()})
        return subs

    graph = []
    for a in _MAIN_AGENTS:
        graph.append(
            {"id": a["id"], "name": a["name"], "type": "main", "sub_agents": _sub_agents(a["id"])}
        )
    return graph


def _registry_agents() -> list[dict[str, Any]]:
    def _list():
        from ...orchestration.agent_registry import get_agent_registry

        return [e.to_dict() for e in get_agent_registry().list_all()]

    return _safe(_list, [])


def _memory_snapshot() -> dict[str, Any]:
    try:
        from ...memory.manager import MemoryManager

        mgr = MemoryManager()
        return {
            "working": len(mgr.working._turns) if hasattr(mgr.working, "_turns") else 0,
            "episodic": len(mgr.episodic._store) if hasattr(mgr.episodic, "_store") else 0,
            "semantic_facts": len(mgr.semantic.facts) if hasattr(mgr.semantic, "facts") else 0,
            "semantic_profiles": len(mgr.semantic.user_profiles)
            if hasattr(mgr.semantic, "user_profiles")
            else 0,
            "procedural": len(mgr.procedural._procedures)
            if hasattr(mgr.procedural, "_procedures")
            else 0,
            "graphiti_enabled": mgr.graphiti is not None,
            "lightrag_enabled": mgr.lightrag is not None,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _monitoring_snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["conversation"] = _safe(
        lambda: dict(
            __import__("deadman.web.server", fromlist=["web_server"]).web_server._conversation_stats
            or {}
        ),
        {},
    )
    out["observability"] = _safe(
        lambda: __import__(
            "deadman.observability.metrics", fromlist=["metrics_collector"]
        ).metrics_collector.get_dashboard(),
        {},
    )
    out["prometheus"] = _safe(
        lambda: {"exporter_available": True, "endpoint": "/metrics"}, {"exporter_available": False}
    )
    out["health"] = _safe(lambda: _health_all_summary(), {})
    return out


def _health_all_summary() -> dict[str, Any]:
    from ...config import settings

    data_dir = settings.project_root / "data"
    domains = [
        "llm",
        "prompt",
        "rule",
        "agent",
        "knowledge",
        "eval",
        "tool",
        "mcp",
        "obs",
        "memory",
        "a2a",
        "deploy",
        "reflexion",
        "skill",
    ]
    summary: dict[str, Any] = {}
    for domain in domains:
        hf = data_dir / f"{domain}_health.json"
        if hf.exists():
            try:
                summary[domain] = json.loads(hf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                summary[domain] = {"status": "parse_error"}
        else:
            summary[domain] = {"status": "no_data"}
    return summary


def _evaluation_snapshot() -> dict[str, Any]:
    from ...config import settings

    out: dict[str, Any] = {}
    out["plan_score"] = _safe(
        lambda: __import__("deadman.plan_score", fromlist=["compute"]).compute_score(), None
    )
    out["selfcheck_available"] = _safe(
        lambda: bool(
            __import__("deadman.selfcheck.checker", fromlist=["SelfCheckChecker"]).SelfCheckChecker
        ),
        False,
    )
    eval_cases = settings.tests_dir / "eval" if hasattr(settings, "tests_dir") else None
    out["case_count"] = _safe(
        lambda: (
            sum(1 for _ in eval_cases.rglob("*.json")) if eval_cases and eval_cases.exists() else 0
        ),
        0,
    )
    return out


@router.get("/overview")
async def admin_overview() -> dict[str, Any]:
    from ...config import settings

    return {
        "service": "deadman 多智能体引导平台",
        "version": getattr(settings, "version", None),
        "llm": _llm_snapshot(),
        "agents": {"main": len(_MAIN_AGENTS), "registry": len(_registry_agents())},
        "tools": _tools_snapshot(),
        "memory": _memory_snapshot(),
        "external_mcp_servers": len(_external_mcp_snapshot()),
        "runtime": _load_runtime(),
    }


@router.get("/config")
async def admin_config() -> dict[str, Any]:
    from ...config import settings

    keys = [
        "llm_provider",
        "llm_model",
        "llm_model_router",
        "llm_model_summarizer",
        "llm_model_respond",
        "memory_max_turns",
        "sandbox_enabled",
        "selfcheck_sample_count",
        "reflexion_max_retries",
        "judge_consensus_threshold",
        "mcp_server_port",
        "web_search_provider",
    ]
    return {
        "runtime_overrides": _load_runtime(),
        "effective": {k: getattr(settings, k, None) for k in keys},
        "env_llm_api_key_set": bool(os.getenv("LLM_API_KEY")),
        "env_llm_provider": os.getenv("LLM_PROVIDER", settings.llm_provider),
        "env_llm_model": os.getenv("LLM_MODEL", settings.llm_model),
    }


@router.get("/models")
async def admin_models() -> dict[str, Any]:
    return {"current": _llm_snapshot(), "catalog": _provider_catalog()}


@router.post("/models")
async def admin_models_set(
    provider: str = Body(default=None, description="provider"),
    model: str = Body(default=None, description="model"),
    api_key: str = Body(default="", description="API key（留空保留现有）"),
    base_url: str = Body(default="", description="base url（留空用默认）"),
) -> dict[str, Any]:
    from ...llm import reconfigure_main_llm

    provider = provider or _llm_snapshot().get("provider")
    model = model or _llm_snapshot().get("model")
    if not api_key:
        api_key = os.getenv("LLM_API_KEY", "")
    resp = reconfigure_main_llm(
        provider=provider, model=model, api_key=api_key or None, base_url=base_url or None
    )
    if not resp.get("ok"):
        raise HTTPException(status_code=400, detail=resp.get("error", "切换失败"))
    runtime = _load_runtime()
    runtime["model"] = {"provider": provider, "model": model}
    _save_runtime(runtime)
    resp["persisted"] = True
    return resp


@router.get("/tools")
async def admin_tools() -> dict[str, Any]:
    return {"local": _tools_snapshot(), "external_mcp": _external_mcp_snapshot()}


@router.post("/tools/{name}/toggle")
async def admin_tool_toggle(name: str, enabled: bool = Body(..., embed=True)) -> dict[str, Any]:
    from ...mcp_server import server as mcp_server_mod

    mcp_server_mod.set_tool_enabled(name, enabled)
    return {"ok": True, "tool": name, "enabled": enabled}


@router.get("/orchestration")
async def admin_orchestration() -> dict[str, Any]:
    def _handoff():
        return {
            "enabled": os.getenv("HANDOFF_ENABLED", "1") != "0",
            "audit_enabled": os.getenv("HANDOFF_AUDIT_ENABLED", "1") != "0",
        }

    def _termination():
        try:
            from ...orchestration.termination import default_termination

            return {"default": str(default_termination())}
        except Exception:
            return {"default": "MAX_STEPS | STUCK_AGENT_REPEAT_LIMIT"}

    return {
        "agents": _agent_graph(),
        "registry_agents": _registry_agents(),
        "handoff": _safe(_handoff, {}),
        "termination": _safe(_termination, {}),
        "external_mcp_servers": _external_mcp_snapshot(),
    }


@router.get("/agents/graph")
async def admin_agents_graph() -> dict[str, Any]:
    return {"graph": _agent_graph(), "registry": _registry_agents()}


@router.get("/monitoring")
async def admin_monitoring() -> dict[str, Any]:
    return _monitoring_snapshot()


@router.get("/evaluation")
async def admin_evaluation() -> dict[str, Any]:
    return _evaluation_snapshot()


@router.get("/error-codes")
async def admin_error_codes() -> dict[str, Any]:
    """GET /api/admin/error-codes —— 统一错误码注册表（deep-spec 21）"""
    from ...errors import ErrorRegistry

    codes = ErrorRegistry.all()
    return {"ok": True, "count": len(codes), "codes": codes}


@router.get("/memory")
async def admin_memory() -> dict[str, Any]:
    return _memory_snapshot()
