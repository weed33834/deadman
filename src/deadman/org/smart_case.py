"""LLM 智能办案助手（P2 功能缺口）：根据案件状态 + 上下文，自动建议下一步动作。

价值：办案员不再手动查"该做什么"，LLM 结合案件类型 / 当前状态 / 客户阶段 /
已有材料，推荐下一步状态迁移、所需材料清单、待办与提示；可一键采纳推进。

降级：LLM 不可用时，按当前状态给规则化建议（仍是可用建议，degraded=True）。

建议结果结构：
    {
        "next_status": "in_progress|pending_input|closed|...",
        "required_materials": [...],
        "actions": [ {label, ...} ],
        "note": "说明",
        "degraded": bool,
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .case_flow import CASE_FLOW

logger = logging.getLogger(__name__)

# 各状态下规则化建议（降级/无 LLM 时）
_RULE_SUGGESTIONS: dict[str, dict[str, Any]] = {
    "created": {
        "next_status": "in_progress",
        "required_materials": ["死亡证明", "身份证明", "户口本"],
        "actions": ["分配办理员", "开始办理"],
        "note": "新案件：先分配办理员并开始办理。",
    },
    "assigned": {
        "next_status": "in_progress",
        "required_materials": ["死亡证明", "客户授权材料"],
        "actions": ["开始办理"],
        "note": "已分配：确认材料并开始办理。",
    },
    "in_progress": {
        "next_status": "pending_input",
        "required_materials": ["补充材料清单", "进度说明"],
        "actions": ["核对材料", "如需补交则置为待补充"],
        "note": "办理中：核对已收材料，缺失则置为待补充。",
    },
    "pending_input": {
        "next_status": "in_progress",
        "required_materials": ["待补材料反馈"],
        "actions": ["核验补交材料", "继续办理"],
        "note": "待补充：核验客户补交材料后继续。",
    },
    "closed": {
        "next_status": "closed",
        "required_materials": [],
        "actions": ["归档完成"],
        "note": "已归档：如需重开请回退到办理中。",
    },
    "cancelled": {
        "next_status": "cancelled",
        "required_materials": [],
        "actions": ["确认取消"],
        "note": "已取消。",
    },
}


def _llm_available() -> bool:
    from ..llm import llm_client

    return bool(getattr(llm_client, "api_key", None))


def _rule_based(case_type: str, status: str) -> dict[str, Any]:
    """规则化建议（无 LLM / 解析失败时降级）。"""
    base = dict(_RULE_SUGGESTIONS.get(status, _RULE_SUGGESTIONS["created"]))
    base["degraded"] = True
    return base


async def suggest_next_action(case: dict[str, Any]) -> dict[str, Any]:
    """根据案件状态 + 类型 + 上下文，建议下一步动作。

    Args:
        case: 案件 dict（至少含 status；可选 case_type / customer_id / note）
    """
    status = case.get("status") or "created"
    case_type = case.get("case_type") or "funeral"
    if status not in CASE_FLOW:
        return _rule_based(case_type, status)

    if not _llm_available():
        return _rule_based(case_type, status)

    from ..llm import llm_client

    prompt = (
        "你是殡葬/身后事办案助手。案件状态=" + status + "，类型=" + case_type + "，"
        "允许的下一步状态=" + str(sorted(CASE_FLOW.get(status, set()))) + "。"
        '输出 JSON：{"next_status":"...","required_materials":[...],'
        '"actions":[{"label":"...","target_status":"..."}],"note":"..."}'
        "next_status 必须来自允许的下一步状态。"
    )
    try:
        out = await llm_client.chat(
            [
                {"role": "system", "content": "你是严谨的办案流程助手，输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        obj = json.loads(out)
        nxt = str(obj.get("next_status", ""))
        allowed = CASE_FLOW.get(status, set())
        if nxt not in allowed and nxt != status:
            # 校验：不允许非法迁移则回退规则建议
            obj["next_status"] = nxt if nxt in allowed else next(iter(allowed), status)
        obj.setdefault("required_materials", [])
        obj.setdefault("actions", [])
        obj.setdefault("note", "")
        obj["degraded"] = False
        return obj
    except Exception as exc:
        logger.warning("smart_case LLM 建议失败，降级规则: %s", exc)
        return _rule_based(case_type, status)


async def generate_case_brief(case: dict[str, Any], customer: dict[str, Any] | None = None) -> str:
    """生成案件简报（供转交 / 材料 / 汇报用）。无 LLM 时降级为模板摘要。"""
    cname = (customer or {}).get("display_name") or (customer or {}).get("name") or "客户"
    if not _llm_available():
        return (
            f"【案件简报】客户：{cname}；案件类型：{case.get('case_type', '-')}；"
            f"状态：{case.get('status', '-')}。"
            f"（LLM 未配置，此为模板摘要，请补充办理进展。）"
        )
    from ..llm import llm_client

    try:
        return await llm_client.chat(
            [
                {"role": "system", "content": "你是殡葬办案助手，撰写简洁案件简报。"},
                {
                    "role": "user",
                    "content": f"客户={cname}，案件={case}，写 80 字以内的办案简报。",
                },
            ],
            temperature=0.3,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("smart_case 简报失败: %s", exc)
        return (
            f"客户：{cname}；类型：{case.get('case_type', '-')}；状态：{case.get('status', '-')}。"
        )
