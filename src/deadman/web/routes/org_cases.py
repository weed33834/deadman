"""机构案件端点 —— /api/org/cases（B2B-IMPLEMENTATION Step 5.2/5.3）

统一依赖 require_org_role("case_manager")。
- 状态机：org/case_flow.py CASE_FLOW，非法迁移 400
- 每次状态变更/分配/材料生成强制落 case_events（审计）
- material：复用 memorial_writer / notification_letters 生成器
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import (
    get_case_event_repo,
    get_case_repo,
    get_customer_repo,
    require_org_role,
)

router = APIRouter(prefix="/api/org/cases", tags=["org-cases"])

# 案件类型/优先级 的枚举 → 中文展示名（前端列表与审计共用）
CASE_TYPE_LABELS = {
    "funeral": "治丧",
    "estate": "遗产继承",
    "insurance": "保险理赔",
    "notarization": "公证",
    "other": "其他",
}
PRIORITY_LABELS = {
    "low": "低",
    "normal": "普通",
    "high": "高",
    "urgent": "紧急",
}


# =====================================================================
# 请求模型
# =====================================================================
class CaseCreate(BaseModel):
    customer_id: str = Field(min_length=1)
    case_type: str = "funeral"  # 治丧/遗产/理赔/公证 ...
    status: str = "created"
    stage: str = ""
    assignee_user_id: str | None = None
    priority: str = "normal"
    source: str = "manual"


class CaseUpdate(BaseModel):
    case_type: str | None = None
    stage: str | None = None
    assignee_user_id: str | None = None
    priority: str | None = None
    source: str | None = None


class StatusChange(BaseModel):
    to_status: str = Field(min_length=1)


class AssignRequest(BaseModel):
    assignee_user_id: str = Field(min_length=1)


class EventAdd(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    detail: dict[str, Any] = {}


class MaterialRequest(BaseModel):
    """材料包生成请求：复用 memorial_writer / notification_letters。"""

    generator: str = "memorial"  # memorial|letter
    doc_type: str = "eulogy"
    decedent_name: str = Field(min_length=1)
    relationship: str = "家属"
    personality_traits: list[str] = []
    memories: list[str] = []
    values_or_sayings: list[str] = []
    tone: str = "solemn"
    faith: str = "none"
    language: str = "zh-CN"
    word_limit: int = 0
    # letter 专属
    letter_type: str = "notice"
    recipient_name: str | None = None


# =====================================================================
# 端点
# =====================================================================
@router.get("")
async def list_cases(
    customer_id: str | None = None,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """机构案件列表（可按客户过滤，case_manager+）。

    附 customer_name（列表展示用）与 case_type_label/priority_label（前端展示）。
    """
    case_repo = get_case_repo()
    rows = await case_repo.list_by_org(ctx["tenant_id"], customer_id=customer_id)
    if rows:
        customer_repo = get_customer_repo()
        names = {
            c["id"]: c.get("display_name", "")
            for c in await customer_repo.list_by_org(ctx["tenant_id"])
        }
        for c in rows:
            c["customer_name"] = names.get(c.get("customer_id", ""), "")
            c["case_type_label"] = CASE_TYPE_LABELS.get(c.get("case_type", ""), c.get("case_type", ""))
            c["priority_label"] = PRIORITY_LABELS.get(c.get("priority", ""), c.get("priority", ""))
    return {"cases": rows, "count": len(rows)}


@router.post("")
async def create_case(
    req: CaseCreate,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """创建案件（case_manager+）；自动落 case.create 事件。"""
    # 校验客户归属：双键 get，跨机构 404
    customer_repo = get_customer_repo()
    customer = await customer_repo.get(ctx["tenant_id"], req.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在或不属于该机构")
    case_repo = get_case_repo()
    try:
        case = await case_repo.create(
            ctx["tenant_id"], req.model_dump(), actor_user_id=ctx["user_id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return case


@router.get("/{case_id}")
async def get_case(
    case_id: str,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """案件详情（双键校验：跨机构 id 返回 404）。"""
    case_repo = get_case_repo()
    case = await case_repo.get(ctx["tenant_id"], case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在或不属于该机构")
    return case


@router.patch("/{case_id}")
async def update_case(
    case_id: str,
    req: CaseUpdate,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """更新案件元数据（case_manager+；status 变更走 /status）。"""
    case_repo = get_case_repo()
    case = await case_repo.update(
        ctx["tenant_id"], case_id, req.model_dump(exclude_none=True)
    )
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在或不属于该机构")
    return case


@router.post("/{case_id}/status")
async def change_case_status(
    case_id: str,
    req: StatusChange,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """状态机流转（case_manager+）；非法迁移 400；强制落事件。"""
    case_repo = get_case_repo()
    try:
        case = await case_repo.update_status(
            ctx["tenant_id"], case_id, req.to_status, actor_user_id=ctx["user_id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在或不属于该机构")
    return case


@router.post("/{case_id}/assign")
async def assign_case(
    case_id: str,
    req: AssignRequest,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """分配案件（org_admin/case_manager）；自动落 case.assign 事件。"""
    case_repo = get_case_repo()
    try:
        case = await case_repo.assign(
            ctx["tenant_id"], case_id, req.assignee_user_id, actor_user_id=ctx["user_id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在或不属于该机构")
    return case


@router.get("/{case_id}/events")
async def list_case_events(
    case_id: str,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """案件时间线/审计（case_manager+，倒序）。"""
    event_repo = get_case_event_repo()
    events = await event_repo.list_by_case(ctx["tenant_id"], case_id)
    return {"events": events, "count": len(events)}


@router.post("/{case_id}/events")
async def add_case_event(
    case_id: str,
    req: EventAdd,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """手动追加事件（case_manager+ 留痕）。"""
    event_repo = get_case_event_repo()
    try:
        event = await event_repo.add(
            ctx["tenant_id"], case_id, ctx["user_id"], req.action, req.detail
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return event


@router.post("/{case_id}/material")
async def generate_material(
    case_id: str,
    req: MaterialRequest,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """生成材料包（case_manager+）：复用 memorial/letters 生成器。

    memorial → MemorialGenerator；letter → LetterGenerator（模板填充不调用 LLM）。
    """
    case_repo = get_case_repo()
    case = await case_repo.get(ctx["tenant_id"], case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在或不属于该机构")

    try:
        if req.generator == "letter":
            result = await _generate_letter(req)
        else:
            result = await _generate_memorial(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"材料生成失败: {exc}") from None

    event_repo = get_case_event_repo()
    await event_repo.add(
        ctx["tenant_id"],
        case_id,
        ctx["user_id"],
        "case.material_generate",
        {"generator": req.generator, "doc_type": req.doc_type},
    )
    return {"case_id": case_id, "material": result}


# =====================================================================
# 生成器封装
# =====================================================================
async def _generate_memorial(req: MaterialRequest) -> dict[str, Any]:
    from ...memorial_writer.generator import MemorialGenerator
    from ...memorial_writer.models import MemorialRequest

    mreq = MemorialRequest(
        doc_type=req.doc_type,
        decedent_name=req.decedent_name,
        relationship=req.relationship,
        personality_traits=list(req.personality_traits),
        memories=list(req.memories),
        values_or_sayings=list(req.values_or_sayings),
        tone=req.tone,
        faith=req.faith,
        language=req.language,
        word_limit=req.word_limit,
    )
    errors = mreq.validate()
    if errors:
        raise ValueError("; ".join(errors))
    result = await MemorialGenerator().generate(mreq)
    return result.to_dict()


async def _generate_letter(req: MaterialRequest) -> dict[str, Any]:
    from ...notification_letters.generator import LetterGenerator
    from ...notification_letters.models import LetterRequest

    lreq = LetterRequest(
        letter_type=req.letter_type,
        decedent_name=req.decedent_name,
        decedent_id_masked="",
        death_date="",
        applicant_name=req.recipient_name or "",
        applicant_relationship="",
        recipient_org="",
        extra_fields={},
        language=req.language,
    )
    result = LetterGenerator().generate(lreq)
    return result.to_dict()
