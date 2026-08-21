"""机构客户档案端点 —— /api/org/customers（B2B-IMPLEMENTATION Step 5.2）

统一依赖 require_org_role("case_manager")：viewer/consultant 无权操作客户。
DELETE 需 org_admin（数据删除权限更高）。

隔离：org_id 一律取自 JWT（require_org_role 返回的 tenant_id），
不接受客户端传 org_id 覆盖；repository 双键校验（org_id + id）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_case_repo, get_customer_repo, require_org_role

router = APIRouter(prefix="/api/org/customers", tags=["org-customers"])


# =====================================================================
# 请求模型
# =====================================================================
class CustomerCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    province: str = ""
    stage: str = "planning"  # planning|funeral|settlement|done
    owner_user_id: str | None = None
    relationships: list[dict[str, Any]] = []
    tags: list[str] = []


class CustomerUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    province: str | None = None
    stage: str | None = None
    owner_user_id: str | None = None
    relationships: list[dict[str, Any]] | None = None
    tags: list[str] | None = None


# =====================================================================
# 端点
# =====================================================================
@router.get("")
async def list_customers(
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """机构客户列表（case_manager+）。"""
    repo = get_customer_repo()
    rows = await repo.list_by_org(ctx["tenant_id"])
    return {"customers": rows, "count": len(rows)}


@router.post("")
async def create_customer(
    req: CustomerCreate,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """创建客户（case_manager+）。"""
    repo = get_customer_repo()
    try:
        customer = await repo.create(
            ctx["tenant_id"], req.model_dump(), actor_user_id=ctx["user_id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return customer


@router.get("/{customer_id}")
async def get_customer(
    customer_id: str,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """客户详情（双键校验：跨机构 id 返回 404）。"""
    repo = get_customer_repo()
    customer = await repo.get(ctx["tenant_id"], customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在或不属于该机构")
    return customer


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: str,
    req: CustomerUpdate,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """更新客户（case_manager+）。"""
    repo = get_customer_repo()
    try:
        customer = await repo.update(
            ctx["tenant_id"], customer_id, req.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在或不属于该机构")
    return customer


@router.delete("/{customer_id}")
async def delete_customer(
    customer_id: str,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """删除客户（仅 org_admin）。"""
    repo = get_customer_repo()
    ok = await repo.delete(ctx["tenant_id"], customer_id)
    if not ok:
        raise HTTPException(status_code=404, detail="客户不存在或不属于该机构")
    return {"deleted": customer_id}


@router.get("/{customer_id}/profile")
async def customer_profile(
    customer_id: str,
    ctx: dict = Depends(require_org_role("case_manager")),
):
    """客户档案聚合：客户 + 关联案件 + 进度汇总（case_manager+）。"""
    customer_repo = get_customer_repo()
    customer = await customer_repo.get(ctx["tenant_id"], customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在或不属于该机构")

    case_repo = get_case_repo()
    cases = await case_repo.list_by_org(ctx["tenant_id"], customer_id=customer_id)
    stages: dict[str, int] = {}
    for c in cases:
        stages[c["status"]] = stages.get(c["status"], 0) + 1
    return {
        "customer": customer,
        "cases": cases,
        "case_count": len(cases),
        "stage_summary": stages,
    }
