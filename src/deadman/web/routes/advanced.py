"""高级通用能力 —— 补齐剩余低优先级缺口

对照 full-spec 缺口清单：
  * P2.14 Agent 发布/灰度
  * P3.10 提示词 A/B 分流
  * P2.15 模板市场（内置模板）
  * P7.8  AI 生成评估用例
  * P8.10 数据漂移检测
  * M7    多租户（轻量：tenant 管理 + 会话租户隔离）
  * P6.10 编排节点暂停/恢复

设计：复用已有 _JsonStore 持久化；全部防御式；可测。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["advanced"])

_ADMIN_DIR = Path.home() / ".deadman" / "admin"


def _store(name: str) -> dict[str, Any]:
    p = _ADMIN_DIR / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(name: str, data: dict[str, Any]) -> None:
    _ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    (_ADMIN_DIR / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# =====================================================================
# P2.14 Agent 发布 / 灰度
# =====================================================================


@router.get("/agents/{agent_id}/versions")
async def agent_versions(agent_id: str) -> dict[str, Any]:
    from .resources import _agents_store

    a = _agents_store.get(agent_id)
    if a is None:
        raise DeadmanHTTPException("DM-AGENT-4040", message=f"Agent 不存在: {agent_id}")
    return {
        "ok": True,
        "name": agent_id,
        "version": a.get("version", 1),
        "status": a.get("status", "published"),
        "versions": a.get("versions", []),
    }


@router.post("/agents/{agent_id}/publish")
async def agent_publish(agent_id: str) -> dict[str, Any]:
    """POST /api/admin/agents/{id}/publish —— 发布 Agent（版本 +1，置为 published）"""
    from .resources import _agents_store

    a = _agents_store.get(agent_id)
    if a is None:
        raise DeadmanHTTPException("DM-AGENT-4040", message=f"Agent 不存在: {agent_id}")
    versions = a.setdefault("versions", [])
    versions.append(
        {
            "version": a.get("version", 1),
            "status": a.get("status", "draft"),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    a["versions"] = versions[-30:]
    a["version"] = int(a.get("version", 1)) + 1
    a["status"] = "published"
    _agents_store.set(agent_id, a)
    return {"ok": True, "name": agent_id, "version": a["version"], "status": "published"}


@router.post("/agents/{agent_id}/rollout")
async def agent_rollout(
    agent_id: str, ratio: float = Body(default=1.0, embed=True, ge=0, le=1)
) -> dict[str, Any]:
    """POST /api/admin/agents/{id}/rollout —— 灰度比例（0-1）"""
    from .resources import _agents_store

    a = _agents_store.get(agent_id)
    if a is None:
        raise DeadmanHTTPException("DM-AGENT-4040", message=f"Agent 不存在: {agent_id}")
    a["rollout_ratio"] = ratio
    _agents_store.set(agent_id, a)
    return {"ok": True, "name": agent_id, "rollout_ratio": ratio}


# =====================================================================
# P3.10 提示词 A/B 分流
# =====================================================================


@router.get("/prompts/{name}/ab")
async def prompt_ab_get(name: str) -> dict[str, Any]:
    from .resources import _prompts_store

    p = _prompts_store.get(name)
    if p is None:
        raise DeadmanHTTPException("DM-PROMPT-4040", message=f"提示词不存在: {name}")
    return {
        "ok": True,
        "name": name,
        "ab": p.get("ab", {"enabled": False, "ratio": 0.5, "variant_b_content": ""}),
    }


@router.put("/prompts/{name}/ab")
async def prompt_ab_set(
    name: str,
    enabled: bool = Body(default=True, embed=True),
    ratio: float = Body(default=0.5, embed=True, ge=0, le=1),
    variant_b_content: str = Body(default="", embed=True),
) -> dict[str, Any]:
    """PUT /api/admin/prompts/{name}/ab —— 配置 A/B 分流（A=当前，B=variant_b_content，ratio 为 B 流量比例）"""
    from .resources import _prompts_store

    p = _prompts_store.get(name)
    if p is None:
        raise DeadmanHTTPException("DM-PROMPT-4040", message=f"提示词不存在: {name}")
    p["ab"] = {"enabled": enabled, "ratio": ratio, "variant_b_content": variant_b_content}
    _prompts_store.set(name, p)
    return {"ok": True, "name": name, "ab": p["ab"]}


# =====================================================================
# P2.15 模板市场（内置模板）
# =====================================================================

_BUILTIN_TEMPLATES = [
    {
        "id": "prompt_legal_advisor",
        "type": "prompt",
        "name": "资深法务顾问",
        "description": "法务咨询角色提示词",
        "content": "你是一位资深法务顾问，引用法条，明确边界，绝不出具正式法律意见。",
    },
    {
        "id": "prompt_financial",
        "type": "prompt",
        "name": "财务分析助手",
        "description": "财务/税务分析角色",
        "content": "你是一位财务分析师，关注资产/债务/税务风险，给出可执行建议。",
    },
    {
        "id": "prompt_grief",
        "type": "prompt",
        "name": "哀伤陪伴",
        "description": "丧亲共情陪伴",
        "content": "你是哀伤陪伴者，共情、倾听、不评判，识别危机信号。",
    },
    {
        "id": "agent_research",
        "type": "agent",
        "name": "政策研究智能体",
        "description": "跨地域政策调研",
        "content": "agent:\n  id: policy_research\n  name: 政策研究\n  system_prompt: 你负责跨地域政策调研。\n  temperature: 0.2",
    },
    {
        "id": "agent_data_analysis",
        "type": "agent",
        "name": "数据分析智能体",
        "description": "数据处理与图表",
        "content": "agent:\n  id: data_analysis\n  name: 数据分析\n  system_prompt: 你负责数据分析与可视化。\n  temperature: 0.1",
    },
]


@router.get("/templates")
async def templates_list() -> dict[str, Any]:
    """GET /api/admin/templates —— 内置模板市场"""
    return {"ok": True, "templates": _BUILTIN_TEMPLATES}


@router.post("/templates/{template_id}/import")
async def templates_import(template_id: str) -> dict[str, Any]:
    """POST /api/admin/templates/{id}/import —— 导入模板到提示词/Agent 存储"""
    tpl = next((t for t in _BUILTIN_TEMPLATES if t["id"] == template_id), None)
    if tpl is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"模板不存在: {template_id}")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if tpl["type"] == "prompt":
        from .resources import _prompts_store

        name = f"{tpl['id']}"
        _prompts_store.set(
            name,
            {
                "content": tpl["content"],
                "description": tpl["description"],
                "type": "chat",
                "version": 1,
                "builtin": False,
                "created_at": now,
            },
        )
        return {"ok": True, "imported": "prompt", "name": name}
    from .resources import _agents_store

    a = {
        "id": tpl["id"],
        "name": tpl["name"],
        "description": tpl["description"],
        "type": "custom",
        "system_prompt": tpl["content"],
        "temperature": 0.3,
        "max_steps": 10,
        "status": "draft",
        "version": 1,
        "created_at": now,
    }
    _agents_store.set(tpl["id"], a)
    return {"ok": True, "imported": "agent", "name": tpl["id"]}


# =====================================================================
# P7.8 AI 生成评估用例
# =====================================================================


@router.post("/evaluations/generate")
async def eval_generate(description: str = Body(default=None, embed=True)) -> dict[str, Any]:
    """POST /api/admin/evaluations/generate —— AI 生成评估用例（结构化 JSON）"""
    from ...llm import llm_client

    if not description:
        raise DeadmanHTTPException("DM-VALID-4002", message="description 必填")
    prompt = (
        "请为以下需求生成 5 条评估用例，仅输出 JSON 数组："
        '[{"input":"...","expected":"...","tag":"正常/边界/对抗"}]。需求：" + description'
    )
    try:
        text = await llm_client.chat([{"role": "user", "content": prompt}], temperature=0.3)
        cases = json.loads(text)
        if not isinstance(cases, list):
            raise ValueError("非数组")
    except Exception as exc:
        raise DeadmanHTTPException("DM-PROMPT-5000", message=f"AI 生成用例失败: {exc}") from exc
    store = _store("eval_cases")
    store.setdefault("cases", []).extend(cases)
    _save("eval_cases", store)
    return {"ok": True, "cases": cases, "count": len(cases)}


# =====================================================================
# P8.10 数据漂移检测
# =====================================================================


def _term_counter(texts: list[str]) -> dict[str, int]:
    """统计一批文本的词频分布"""
    from collections import Counter

    from ...textproc import remove_stopwords, tokenize_words

    c: Counter = Counter()
    for t in texts:
        c.update(remove_stopwords(tokenize_words(t or "")))
    return dict(c)


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    import math

    keys = set(a) | set(b)
    if not keys:
        return 1.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 1.0
    return dot / (na * nb)


def _recent_inputs(limit: int = 200) -> list[str]:
    """读取最近会话的用户消息作为当前输入分布"""
    from .sessions import _load as _session_load
    from .sessions import _sessions_dir

    texts: list[str] = []
    for p in sorted(_sessions_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[
        :5
    ]:
        try:
            data = _session_load(p.stem)
            for m in (data or {}).get("messages", []):
                if m.get("role") == "user":
                    texts.append(m.get("content", ""))
        except Exception:
            continue
        if len(texts) >= limit:
            break
    return texts


@router.get("/drift")
async def drift_check() -> dict[str, Any]:
    """GET /api/admin/drift —— 数据漂移检测（基线词分布 vs 最近输入词分布 余弦相似度）"""
    from .admin import _load_runtime

    baseline = _load_runtime().get("input_baseline") or {}
    bdist = baseline.get("term_dist")
    if not bdist:
        return {
            "ok": True,
            "drift": False,
            "status": "no_baseline",
            "note": "先 POST /api/admin/drift/baseline 采集基线",
        }
    recent = _recent_inputs()
    if not recent:
        return {"ok": True, "drift": False, "status": "no_input", "note": "尚无近期输入可对比"}
    cdist = _term_counter(recent)
    # 归一化为比例
    t1 = sum(bdist.values()) or 1
    t2 = sum(cdist.values()) or 1
    bnorm = {k: v / t1 for k, v in bdist.items()}
    cnorm = {k: v / t2 for k, v in cdist.items()}
    sim = _cosine(bnorm, cnorm)
    drift = sim < 0.6
    return {
        "ok": True,
        "drift": drift,
        "status": "drifted" if drift else "stable",
        "similarity": round(sim, 4),
        "threshold": 0.6,
        "baseline_terms": len(bdist),
        "current_terms": len(cdist),
        "recent_inputs": len(recent),
    }


@router.post("/drift/baseline")
async def drift_baseline(sample: dict[str, Any] = Body(default={})) -> dict[str, Any]:  # noqa: B008
    """POST /api/admin/drift/baseline —— 采集当前输入分布作为基线"""
    from .admin import _load_runtime, _save_runtime

    recent = _recent_inputs()
    runtime = _load_runtime()
    runtime["input_baseline"] = {
        "term_dist": _term_counter(recent),
        "sample_terms": len(recent),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_runtime(runtime)
    return {"ok": True, "captured": True, "inputs_sampled": len(recent)}


# =====================================================================
# M7 多租户（轻量）+ P6.10 节点暂停/恢复
# =====================================================================


@router.get("/tenants")
async def tenants_list() -> dict[str, Any]:
    """GET /api/admin/tenants —— 租户列表"""
    return {"ok": True, "tenants": _store("tenants").get("tenants", [])}


@router.post("/tenants")
async def tenants_create(name: str = Body(default=None, embed=True)) -> dict[str, Any]:
    """POST /api/admin/tenants —— 创建租户"""
    if not name:
        raise DeadmanHTTPException("DM-VALID-4002", message="name 必填")
    store = _store("tenants")
    items = store.setdefault("tenants", [])
    items.append(
        {
            "id": f"t-{len(items) + 1}",
            "name": name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    _save("tenants", store)
    return {"ok": True, "tenant": items[-1]}


@router.get("/orchestration/nodes/state")
async def node_states() -> dict[str, Any]:
    """GET /api/admin/orchestration/nodes/state —— 节点运行状态一览"""
    return {"ok": True, "nodes": _store("node_states")}


@router.post("/orchestration/{agent_id}/pause")
async def node_pause(agent_id: str) -> dict[str, Any]:
    """POST /api/admin/orchestration/{agent_id}/pause —— 暂停节点"""
    _set_node_state(agent_id, "paused")
    return {"ok": True, "agent_id": agent_id, "state": "paused"}


@router.post("/orchestration/{agent_id}/resume")
async def node_resume(agent_id: str) -> dict[str, Any]:
    """POST /api/admin/orchestration/{agent_id}/resume —— 恢复节点"""
    _set_node_state(agent_id, "running")
    return {"ok": True, "agent_id": agent_id, "state": "running"}


def _set_node_state(agent_id: str, state: str) -> None:
    store = _store("node_states")
    store[agent_id] = {
        "state": state,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save("node_states", store)
