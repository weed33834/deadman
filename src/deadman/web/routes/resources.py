"""资源服务 —— /api/admin/* 真正可增删改调的管理接口（对齐 full-spec / admin-console-design）

把管理台从"只读监控"升级为"资源管理"：
  * Prompt Manager：创建/更新/删除/AI 生成/测试台试跑
  * Tool TestRunner：对任意工具填参试跑，返回结果+耗时+审计
  * Model 连通性测试：ping 指定 provider/model
  * Agent Manager：创建/更新/删除/试跑（走完整对话管线）
  * Voice Manager：音色资产 CRUD/设默认
  * Settings：可视化编辑 env 子集（热加载 + 持久化 .env）
  * Backup：全量配置包导出/导入

持久化目录：``~/.deadman/admin/*.json``
设计原则：写操作返回结构化结果；底层异常防御式降级；后端有接口 → 前端必有入口。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-resources"])

_ADMIN_DIR = Path.home() / ".deadman" / "admin"


class _JsonStore:
    """极简 JSON 持久化存储（每个资源一个文件）"""

    def __init__(self, filename: str):
        self.path = _ADMIN_DIR / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def items(self) -> dict[str, Any]:
        return self._load()

    def get(self, key: str) -> Any | None:
        return self._load().get(key)

    def set(self, key: str, value: Any) -> None:
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> bool:
        data = self._load()
        removed = key in data
        if removed:
            del data[key]
            self._save(data)
        return removed


_prompts_store = _JsonStore("prompts.json")
_agents_store = _JsonStore("agents.json")
_voices_store = _JsonStore("voices.json")
_tool_runs_store = _JsonStore("tool_runs.json")


def _esc(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "_-.").strip(".") or "untitled"


# =====================================================================
# P 提示词管理
# =====================================================================


def _builtin_prompts() -> list[dict[str, Any]]:
    from ...config import settings

    items: list[dict[str, Any]] = []
    for d in (settings.agents_dir, settings.rules_dir):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            items.append(
                {
                    "name": f.stem,
                    "type": "rule" if f.parent == settings.rules_dir else "agent",
                    "path": str(f),
                    "builtin": True,
                    "content": f.read_text(encoding="utf-8"),
                }
            )
    return items


@router.get("/prompts")
async def list_prompts() -> dict[str, Any]:
    builtin = _builtin_prompts()
    user_items = [{"name": k, **v} for k, v in _prompts_store.items().items()]
    return {
        "prompts": user_items,
        "rules": [p for p in builtin if p["type"] == "rule"],
        "agents": [p for p in builtin if p["type"] == "agent"],
        "all": user_items + builtin,
    }


@router.post("/prompts")
async def create_prompt(
    name: str = Body(default=None, description="名称"),
    content: str = Body(default=None, description="内容"),
    description: str = Body(default="", description="描述"),
    prompt_type: str = Body(default="chat", description="类型"),
) -> dict[str, Any]:
    if not name or content is None:
        raise DeadmanHTTPException("DM-PROMPT-4001")
    safe = _esc(name)
    data = {
        "content": content,
        "description": description,
        "type": prompt_type,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1,
        "builtin": False,
    }
    _prompts_store.set(safe, data)
    return {"ok": True, "name": safe, "prompt": data}


@router.get("/prompts/{name}")
async def get_prompt(name: str) -> dict[str, Any]:
    user = _prompts_store.get(name)
    if user is not None:
        return {"name": name, **user}
    for p in _builtin_prompts():
        if p["name"] == name:
            return p
    raise DeadmanHTTPException("DM-PROMPT-4040", message=f"未找到提示词: {name}")


@router.put("/prompts/{name}")
async def update_prompt(
    name: str,
    content: str = Body(default=None, description="内容"),
    description: str = Body(default=""),
) -> dict[str, Any]:
    if content is None:
        raise DeadmanHTTPException("DM-PROMPT-4001", message="content 必填")
    existing = _prompts_store.get(name) or {}
    # 版本历史：保存旧版本（用于 diff / 回滚，M2）
    versions = existing.setdefault("versions", [])
    versions.append(
        {
            "version": int(existing.get("version", 0)),
            "content": existing.get("content", ""),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    existing["versions"] = versions[-50:]  # 最多保留 50 个历史版本
    existing["content"] = content
    if description:
        existing["description"] = description
    existing["version"] = int(existing.get("version", 0)) + 1
    existing["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _prompts_store.set(name, existing)
    return {"ok": True, "name": name, "version": existing["version"]}


@router.get("/prompts/{name}/versions")
async def prompt_versions(name: str) -> dict[str, Any]:
    """GET /api/admin/prompts/{name}/versions —— 版本历史（供 diff/回滚）"""
    p = _prompts_store.get(name)
    if p is None:
        raise DeadmanHTTPException("DM-PROMPT-4040", message=f"未找到提示词: {name}")
    return {
        "ok": True,
        "name": name,
        "current_version": p.get("version", 0),
        "versions": p.get("versions", []),
    }


@router.post("/prompts/{name}/rollback")
async def prompt_rollback(
    name: str, version: int = Body(default=None, embed=True)
) -> dict[str, Any]:
    """POST /api/admin/prompts/{name}/rollback —— 回滚到指定版本"""
    p = _prompts_store.get(name)
    if p is None:
        raise DeadmanHTTPException("DM-PROMPT-4040", message=f"未找到提示词: {name}")
    versions = p.get("versions", [])
    target = next((v for v in versions if v.get("version") == version), None)
    if target is None:
        raise DeadmanHTTPException("DM-PROMPT-4040", message=f"版本 {version} 不存在")
    # 回滚：当前内容入历史，恢复目标版本内容为新版本
    versions.append(
        {
            "version": p.get("version", 0),
            "content": p.get("content", ""),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    p["versions"] = versions[-50:]
    p["content"] = target["content"]
    p["version"] = int(p.get("version", 0)) + 1
    p["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _prompts_store.set(name, p)
    return {"ok": True, "name": name, "version": p["version"], "rolled_back_to": version}


@router.delete("/prompts/{name}")
async def delete_prompt(name: str) -> dict[str, Any]:
    if _prompts_store.delete(name):
        return {"ok": True, "name": name, "deleted": True}
    raise DeadmanHTTPException("DM-PROMPT-4090", message="内置提示词不可删除，或不存在")


@router.post("/prompts/generate")
async def generate_prompt(
    description: str = Body(default=None, description="自然语言描述"),
    base: str = Body(default="", description="可选：基于其优化"),
    action: str = Body(
        default="generate", description="generate/optimize/rewrite/translate/review"
    ),
) -> dict[str, Any]:
    from ...llm import llm_client

    _action_prompts = {
        "generate": "根据以下描述，生成一份专业、结构化的中文 system prompt，包含职责、规则、边界与输出要求：\n{desc}",
        "optimize": "请优化以下提示词（按最佳实践：更明确、更少歧义、更稳输出）：\n\n{desc}",
        "rewrite": "请改写以下提示词的语气，使其更专业友好，保留所有变量占位符 {var} 不变：\n\n{desc}",
        "translate": "请把以下提示词翻译成中文，保留所有 {变量} 占位符不变：\n\n{desc}",
        "review": "请审查以下提示词的安全与质量：指出指令冲突、越权、提示注入风险，并给出改进建议：\n\n{desc}",
    }
    user_prompt = _action_prompts.get(action, _action_prompts["generate"]).format(
        desc=description or ""
    )
    try:
        content = await llm_client.chat([{"role": "user", "content": user_prompt}], temperature=0.3)
    except Exception as exc:
        raise DeadmanHTTPException("DM-PROMPT-5000", message=f"AI 生成失败: {exc}") from exc
    drafts = _prompts_store.get("__drafts__") or []
    draft = {
        "name": f"draft_{int(time.time())}",
        "action": action,
        "content": content,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    drafts.append(draft)
    _prompts_store.set("__drafts__", drafts)
    return {"ok": True, "draft": draft, "saved_as_draft": True}


@router.post("/prompts/{name}/test")
async def test_prompt(
    name: str, input_text: str = Body(default=None, embed=True, description="测试输入")
) -> dict[str, Any]:
    from ...llm import llm_client

    p = _prompts_store.get(name)
    if p is None:
        for b in _builtin_prompts():
            if b["name"] == name:
                p = b
                break
    if p is None:
        raise DeadmanHTTPException("DM-PROMPT-4040", message=f"未找到提示词: {name}")
    system = p.get("content", "")
    start = time.monotonic()
    try:
        text = await llm_client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": input_text or "你好"},
            ],
            temperature=0.3,
        )
        return {
            "ok": True,
            "name": name,
            "output": text,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
    except Exception as exc:
        return {"ok": False, "name": name, "error": str(exc)}


# =====================================================================
# T 工具测试台
# =====================================================================


@router.post("/tools/test")
async def tool_test(
    name: str = Body(default=None, description="工具名"),
    arguments: dict[str, Any] = Body(default=None, description="测试参数"),  # noqa: B008
) -> dict[str, Any]:
    from ...mcp_server.server import mcp

    if not name:
        raise DeadmanHTTPException("DM-TOOL-4001")
    arguments = arguments or {}
    start = time.monotonic()
    result = await mcp.call_tool(name, arguments)
    duration_ms = int((time.monotonic() - start) * 1000)
    runs = _tool_runs_store.get("runs") or []
    runs.append(
        {
            "tool": name,
            "arguments": {k: ("***" if "key" in k.lower() else v) for k, v in arguments.items()},
            "ok": result.get("ok", False),
            "duration_ms": duration_ms,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    _tool_runs_store.set("runs", runs[-200:])
    return {
        "ok": result.get("ok", False),
        "tool": name,
        "result": result,
        "duration_ms": duration_ms,
    }


@router.get("/tools/runs")
async def tool_runs() -> dict[str, Any]:
    return {"runs": _tool_runs_store.get("runs") or []}


# =====================================================================
# M 模型连通性测试
# =====================================================================


@router.post("/models/test")
async def model_test(
    provider: str = Body(default=None, description="provider"),
    model: str = Body(default=None, description="model"),
    api_key: str = Body(default="", description="api key（可选）"),
    base_url: str = Body(default="", description="base url（可选）"),
) -> dict[str, Any]:
    from ...llm import LLMClient

    client = LLMClient(
        provider=provider or "",
        model=model or "",
        api_key=api_key or os.getenv("LLM_API_KEY", ""),
        base_url=base_url or "",
    )
    start = time.monotonic()
    try:
        resp = await client.ping_once()
        return {
            "ok": True,
            "provider": provider,
            "model": model,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "reply": (resp.content or "")[:80],
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": str(exc),
        }


# =====================================================================
# A Agent 管理
# =====================================================================

_DEFAULT_AGENT_TEMPLATE = {
    "type": "custom",
    "description": "自定义智能体",
    "system_prompt": "你是一位专业的助手，请认真、准确、负责地回答用户问题。",
    "model": "",
    "temperature": 0.3,
    "max_steps": 10,
    "tools": [],
    "enabled": True,
}


def _builtin_agents() -> list[dict[str, str]]:
    return [
        {"id": "death-aftercare"},
        {"id": "legal-advisor"},
        {"id": "financial-analyst"},
        {"id": "policy-researcher"},
        {"id": "cross-border-specialist"},
        {"id": "medical-guide"},
    ]


@router.get("/agents")
async def list_agents() -> dict[str, Any]:
    builtin = [
        {"id": a["id"], "name": _MAIN_AGENT_NAMES.get(a["id"], a["id"]), "type": "builtin"}
        for a in _builtin_agents()
    ]
    custom = [{"id": k, **v} for k, v in _agents_store.items().items()]
    return {"agents": builtin + custom}


_MAIN_AGENT_NAMES = {
    "death-aftercare": "身后事流程引导员",
    "legal-advisor": "法律咨询智能体",
    "financial-analyst": "财务分析智能体",
    "policy-researcher": "政策研究智能体",
    "cross-border-specialist": "跨境事务智能体",
    "medical-guide": "医疗导航智能体",
}


@router.post("/agents")
async def create_agent(
    agent: dict[str, Any] = Body(default=None, description="Agent 配置"),  # noqa: B008
) -> dict[str, Any]:
    agent = agent or {}
    if not agent.get("id") and not agent.get("name"):
        raise DeadmanHTTPException("DM-AGENT-4001")
    agent_id = _esc(str(agent.get("id") or agent.get("name")))
    payload = dict(_DEFAULT_AGENT_TEMPLATE)
    payload.update(agent)
    payload["id"] = agent_id
    payload["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _agents_store.set(agent_id, payload)
    return {"ok": True, "agent": payload}


@router.post("/agents/generate")
async def generate_agent(
    description: str = Body(default=None, embed=True, description="自然语言描述"),
) -> dict[str, Any]:
    """POST /api/admin/agents/generate —— AI 生成 Agent（描述 → yaml 草稿）

    围绕产品特色：输入"帮我做一个能写周报的 agent"等，生成 agent.yaml 配置草稿。
    """
    from ...llm import llm_client

    if not description:
        raise DeadmanHTTPException("DM-VALID-4002", message="description 必填")
    prompt = (
        "你是 Agent 配置生成器。根据用户描述，生成一份完整的 agent.yaml（含 name/type/description/"
        "system_prompt/temperature/max_steps/tools/voice）。只输出 YAML，不要多余说明。\n\n描述："
        + description
    )
    try:
        yaml_text = await llm_client.chat([{"role": "user", "content": prompt}], temperature=0.3)
    except Exception as exc:
        raise DeadmanHTTPException("DM-PROMPT-5000", message=f"AI 生成失败: {exc}") from exc
    # 存为草稿
    drafts = _agents_store.get("__drafts__") or []
    draft = {
        "name": f"agent_draft_{int(time.time())}",
        "description": description,
        "yaml": yaml_text,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    drafts.append(draft)
    _agents_store.set("__drafts__", drafts)
    return {"ok": True, "draft": draft, "agent_yaml": yaml_text}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict[str, Any]:
    if agent_id in {a["id"] for a in _builtin_agents()}:
        raise DeadmanHTTPException("DM-AGENT-4090")
    if _agents_store.delete(agent_id):
        return {"ok": True, "agent_id": agent_id, "deleted": True}
    raise DeadmanHTTPException("DM-AGENT-4040", message=f"未找到 Agent: {agent_id}")


@router.post("/agents/{agent_id}/test")
async def test_agent(agent_id: str, query: str = Body(default=None, embed=True)) -> dict[str, Any]:
    from .server import web_server

    start = time.monotonic()
    try:
        resp = await web_server._handle_chat(agent_id, query or "你好", None, None)
        resp["duration_ms"] = int((time.monotonic() - start) * 1000)
        return resp
    except Exception as exc:
        return {"ok": False, "agent_id": agent_id, "error": str(exc)}


# =====================================================================
# V 音色资产
# =====================================================================


def _default_voices() -> list[dict[str, Any]]:
    return [
        {
            "id": "gentle_male",
            "name": "温和男声",
            "engine": "default",
            "voice_id": "gentle_male",
            "language": "zh-CN",
            "gender": "male",
            "style": ["温和", "悼文"],
            "params": {"rate": 1.0, "pitch": 1.0, "volume": 1.0},
            "is_system": True,
            "is_default": True,
            "status": "ready",
        },
        {
            "id": "gentle_female",
            "name": "温和女声",
            "engine": "default",
            "voice_id": "gentle_female",
            "language": "zh-CN",
            "gender": "female",
            "style": ["温和", "家书"],
            "params": {"rate": 1.0, "pitch": 1.0, "volume": 1.0},
            "is_system": True,
            "is_default": False,
            "status": "ready",
        },
        {
            "id": "professional_male",
            "name": "正式男声",
            "engine": "default",
            "voice_id": "professional_male",
            "language": "zh-CN",
            "gender": "male",
            "style": ["正式", "公告"],
            "params": {"rate": 1.0, "pitch": 1.0, "volume": 1.0},
            "is_system": True,
            "is_default": False,
            "status": "ready",
        },
        {
            "id": "professional_female",
            "name": "正式女声",
            "engine": "default",
            "voice_id": "professional_female",
            "language": "zh-CN",
            "gender": "female",
            "style": ["正式", "通知"],
            "params": {"rate": 1.0, "pitch": 1.0, "volume": 1.0},
            "is_system": True,
            "is_default": False,
            "status": "ready",
        },
    ]


@router.get("/voices")
async def list_voices() -> dict[str, Any]:
    custom = [{"id": k, **v} for k, v in _voices_store.items().items()]
    return {"voices": _default_voices() + custom}


@router.post("/voices")
async def create_voice(
    voice: dict[str, Any] = Body(default=None, description="音色配置"),  # noqa: B008
) -> dict[str, Any]:
    voice = voice or {}
    vid = _esc(str(voice.get("id") or voice.get("name") or "voice"))
    payload = {
        "name": voice.get("name") or vid,
        "engine": voice.get("engine") or "default",
        "voice_id": voice.get("voice_id") or vid,
        "language": voice.get("language") or "zh-CN",
        "gender": voice.get("gender") or "female",
        "style": voice.get("style") or [],
        "params": voice.get("params") or {"rate": 1.0, "pitch": 1.0, "volume": 1.0},
        "is_system": False,
        "is_default": False,
        "status": voice.get("status") or "ready",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _voices_store.set(vid, payload)
    return {"ok": True, "voice": {"id": vid, **payload}}


@router.put("/voices/{voice_id}")
async def update_voice(
    voice_id: str,
    voice: dict[str, Any] = Body(default=None, description="音色配置"),  # noqa: B008
) -> dict[str, Any]:
    existing = _voices_store.get(voice_id)
    if existing is None:
        raise DeadmanHTTPException("DM-VOICE-4040", message=f"未找到音色: {voice_id}")
    voice = voice or {}
    for k in ("name", "engine", "voice_id", "language", "gender", "style", "params", "status"):
        if k in voice:
            existing[k] = voice[k]
    _voices_store.set(voice_id, existing)
    return {"ok": True, "voice_id": voice_id, "voice": existing}


@router.delete("/voices/{voice_id}")
async def delete_voice(voice_id: str) -> dict[str, Any]:
    if _voices_store.delete(voice_id):
        return {"ok": True, "voice_id": voice_id, "deleted": True}
    raise DeadmanHTTPException("DM-VOICE-4090", message="预置音色不可删除，或不存在")


@router.post("/voices/{voice_id}/set-default")
async def voice_set_default(voice_id: str) -> dict[str, Any]:
    _voices_store.set("__default__", voice_id)
    return {"ok": True, "default_voice_id": voice_id}


# =====================================================================
# S 系统设置 + 备份
# =====================================================================

_EDITABLE_ENV_KEYS = [
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_MODEL_ROUTER",
    "LLM_MODEL_SUMMARIZER",
    "LLM_MODEL_RESPOND",
    "MEMORY_MAX_TURNS",
    "WEB_SEARCH_PROVIDER",
    "DEADMAN_MULTIMODAL_ENABLED",
    "SANDBOX_ENABLED",
    "REFLEXION_MAX_RETRIES",
]


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    from .admin import _load_runtime

    settings_view = {}
    for key in _EDITABLE_ENV_KEYS:
        val = os.getenv(key, "")
        settings_view[key] = {"value": val, "masked": bool("KEY" in key and val)}
    settings_view["sensitive_masked"] = True
    return {"env": settings_view, "runtime": _load_runtime()}


@router.put("/settings")
async def put_settings(
    env: dict[str, Any] = Body(default=None, description="env 配置"),  # noqa: B008
) -> dict[str, Any]:
    env = env or {}
    written: dict[str, bool] = {}
    env_path = Path(__file__).parent.parent.parent / ".env"
    for key, val in env.items():
        if key not in _EDITABLE_ENV_KEYS:
            continue
        value = str(val.get("value") if isinstance(val, dict) else val)
        os.environ[key] = value
        try:
            if env_path.exists():
                text = env_path.read_text(encoding="utf-8")
                if re.search(rf"^{re.escape(key)}=", text, re.M):
                    text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.M)
                else:
                    text += f"\n{key}={value}\n"
                env_path.write_text(text, encoding="utf-8")
                written[key] = True
        except OSError:
            written[key] = False
    return {"ok": True, "written": written, "note": "已热加载；已持久化到 .env"}


@router.get("/backup/export")
async def backup_export() -> dict[str, Any]:
    from ...mcp_server.client import get_client_manager
    from .admin import _load_runtime

    package = {
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prompts": _prompts_store.items(),
        "agents": _agents_store.items(),
        "voices": {k: v for k, v in _voices_store.items().items() if isinstance(v, dict)},
        "tool_runs": _tool_runs_store.items(),
        "model_override": _load_runtime().get("model"),
        "mcp_clients": get_client_manager().list_servers(),
    }
    return {"ok": True, "package": package}


@router.post("/backup/import")
async def backup_import(
    package: dict[str, Any] = Body(default=None, description="配置包"),  # noqa: B008
) -> dict[str, Any]:
    package = package or {}
    imported: list[str] = []
    if isinstance(package.get("prompts"), dict):
        for k, v in package["prompts"].items():
            if k == "__drafts__":
                continue
            _prompts_store.set(k, v)
        imported.append(f"prompts({len(package['prompts'])})")
    if isinstance(package.get("agents"), dict):
        for k, v in package["agents"].items():
            _agents_store.set(k, v)
        imported.append(f"agents({len(package['agents'])})")
    if isinstance(package.get("voices"), dict):
        for k, v in package["voices"].items():
            if isinstance(v, dict) and not v.get("is_system"):
                _voices_store.set(k, v)
        imported.append(f"voices({len(package['voices'])})")
    return {"ok": True, "imported": imported}
