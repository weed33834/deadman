"""To B 机构上下文端点 —— /api/orgs/*

能力（对齐 B2B-IMPLEMENTATION Step 3.2）：
  - POST /api/orgs/switch：切换当前机构，重签带 tenant_id/org_role 的 JWT
  - GET  /api/orgs/memberships：列出当前用户所有机构关联（登录后选机构）
  - GET  /api/orgs/me：返回当前 JWT 中的机构上下文

所有端点依赖已登录用户（get_current_user），机构内权限由
require_org_role 在业务端点上另行约束。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..deps import (
    get_current_user,
    get_invite_store,
    get_jwt_manager,
    get_org_store,
    require_admin,
    require_org_role,
)

router = APIRouter(prefix="/api/orgs", tags=["orgs"])

_bearer = HTTPBearer(auto_error=False)


class OrgSwitchRequest(BaseModel):
    org_id: str = Field(min_length=1, description="目标机构 id")


class OrgCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=60)
    industry_template: str = Field(default="funeral", max_length=40)
    plan: str = Field(default="free", max_length=20)


class OrgUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    status: str | None = None
    plan: str | None = None


class OrgInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    role: str = Field(default="viewer", max_length=20)


class OrgInviteAccept(BaseModel):
    token: str = Field(min_length=8, max_length=200)


class MemberUpdateRequest(BaseModel):
    org_role: str | None = Field(default=None, max_length=20)
    status: str | None = Field(default=None, max_length=20)


def _token_expiry_iso(jwt_mgr, token: str) -> str:
    """解析 token exp 为 ISO 时间戳（与 web/app.py 的辅助函数一致）"""
    payload = jwt_mgr.verify(token)
    if payload is None:
        return ""
    try:
        return datetime.fromtimestamp(
            payload.get("exp", 0), tz=timezone.utc
        ).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


@router.post("/switch")
async def orgs_switch(
    req: OrgSwitchRequest,
    user: dict = Depends(get_current_user),
):
    """切换当前机构：校验 membership active 后重签 JWT。

    - 401：未登录
    - 404：机构不存在或已停用
    - 403：非该机构 active 成员
    """
    org_store = get_org_store()
    org = org_store.get_org(req.org_id)
    if org is None or not org.is_active():
        raise HTTPException(status_code=404, detail="机构不存在或已停用")
    membership = org_store.get_membership(req.org_id, user["user_id"])
    if membership is None or not membership.is_active():
        raise HTTPException(status_code=403, detail="非机构成员或已被禁用")
    jwt_mgr = get_jwt_manager()
    token = jwt_mgr.switch_org(user, req.org_id, membership.org_role)
    return {
        "token": token,
        "expires_at": _token_expiry_iso(jwt_mgr, token),
        "tenant_id": req.org_id,
        "org_role": membership.org_role,
        "org_name": org.name,
    }


@router.get("/memberships")
async def orgs_memberships(user: dict = Depends(get_current_user)):
    """列出当前用户的所有机构关联（含 disabled，前端可提示重新邀请）。"""
    org_store = get_org_store()
    memberships = org_store.list_user_orgs(user["user_id"])
    rows = []
    for m in memberships:
        d = m.to_dict()
        org = org_store.get_org(m.org_id)
        d["org_name"] = org.name if org is not None else ""
        d["org_slug"] = org.slug if org is not None else ""
        rows.append(d)
    return {
        "memberships": rows,
        "count": len(rows),
    }


def _require_platform_admin(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    return require_admin(cred, x_admin_token, strict=True)


@router.post("", status_code=201)
async def orgs_create(
    req: OrgCreateRequest,
    admin: dict = Depends(_require_platform_admin),
):
    """创建机构（platform_admin）。

    设计对齐 B2B-TECH-DESIGN §6.1：仅 platform_admin 可创建机构。
    创建后机构无成员，由创建者（或平台方）邀请成员加入。
    """
    org_store = get_org_store()
    try:
        org = org_store.create_org(
            name=req.name,
            slug=req.slug,
            industry_template=req.industry_template,
            plan=req.plan,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return org.to_dict()


@router.patch("/{org_id}")
async def orgs_update(
    org_id: str,
    req: OrgUpdateRequest,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """更新机构资料（org_admin）。"""
    if ctx["tenant_id"] != org_id:
        raise HTTPException(status_code=403, detail="只能更新当前机构")
    org_store = get_org_store()
    fields = req.model_dump(exclude_none=True)
    try:
        org = org_store.update_org(org_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if org is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    return org.to_dict()


@router.post("/{org_id}/members/invite", status_code=201)
async def orgs_members_invite(
    org_id: str,
    req: OrgInviteRequest,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """生成成员邀请令牌（org_admin）。"""
    if ctx["tenant_id"] != org_id:
        raise HTTPException(status_code=403, detail="只能邀请加入当前机构")
    invite_store = get_invite_store()
    try:
        token = invite_store.create_invite(
            org_id, req.email, req.role, invited_by=ctx["user_id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"token": token, "org_id": org_id, "email": req.email, "role": req.role}


@router.post("/invites/accept")
async def orgs_invites_accept(
    req: OrgInviteAccept,
    user: dict = Depends(get_current_user),
):
    """用户凭邀请令牌加入机构（登录态，令牌单次使用）。

    先 peek 校验邮箱归属再 consume，避免「错误账号试探」烧掉有效令牌。
    """
    invite_store = get_invite_store()
    info = invite_store.peek_invite(req.token)
    if info is None:
        raise HTTPException(status_code=404, detail="邀请令牌无效或已过期")
    if (info.get("email") or "").lower() != (user.get("email") or "").lower():
        raise HTTPException(status_code=403, detail="邀请令牌与当前登录账号不匹配")
    consumed = invite_store.consume_invite(req.token)
    if consumed is None:
        raise HTTPException(status_code=404, detail="邀请令牌无效或已过期")
    org_store = get_org_store()
    org = org_store.get_org(info["org_id"])
    if org is None or not org.is_active():
        raise HTTPException(status_code=404, detail="机构不存在或已停用")
    membership = org_store.add_member(
        org_id=info["org_id"],
        user_id=user["user_id"],
        org_role=info.get("role", "viewer"),
        invited_by=info.get("invited_by"),
    )
    return {
        "org_id": info["org_id"],
        "org_name": org.name,
        "org_role": membership.org_role,
        "status": membership.status,
    }


@router.patch("/{org_id}/members/{member_user_id}")
async def orgs_members_update(
    org_id: str,
    member_user_id: str,
    req: MemberUpdateRequest,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """修改成员角色/状态（org_admin，仅限当前机构）。"""
    if ctx["tenant_id"] != org_id:
        raise HTTPException(status_code=403, detail="只能管理当前机构成员")
    if member_user_id == ctx["user_id"] and req.status == "disabled":
        raise HTTPException(status_code=400, detail="不能停用自己的账号")
    org_store = get_org_store()
    try:
        if req.org_role is not None:
            member = org_store.set_member_role(org_id, member_user_id, req.org_role)
        else:
            member = org_store.get_membership(org_id, member_user_id)
        if req.status is not None and member is not None:
            member = org_store.set_member_status(org_id, member_user_id, req.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    return member.to_dict()


@router.delete("/{org_id}/members/{member_user_id}")
async def orgs_members_remove(
    org_id: str,
    member_user_id: str,
    ctx: dict = Depends(require_org_role("org_admin")),
):
    """移除成员（org_admin，客户数据保留在机构）。"""
    if ctx["tenant_id"] != org_id:
        raise HTTPException(status_code=403, detail="只能管理当前机构成员")
    if member_user_id == ctx["user_id"]:
        raise HTTPException(status_code=400, detail="不能移除自己")
    org_store = get_org_store()
    if not org_store.remove_member(org_id, member_user_id):
        raise HTTPException(status_code=404, detail="成员不存在")
    return {"removed": True, "org_id": org_id, "user_id": member_user_id}


@router.get("/me")
async def orgs_me(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """返回当前 JWT 中的机构上下文（未登录/未绑定则为 None）。

    返回 `org` 字段（机构资料）供工作台顶栏/设置页展示；memberships 由
    ``/api/orgs/memberships`` 提供。
    """
    payload = None
    if cred:
        payload = get_jwt_manager().verify(cred.credentials)
    if payload is None:
        return {"user_id": None, "tenant_id": None, "org_role": None, "org": None}
    org = None
    tenant_id = payload.get("tenant_id")
    if tenant_id:
        org_store = get_org_store()
        found = org_store.get_org(tenant_id)
        if found is not None:
            org = found.to_dict()
    return {
        "user_id": payload.get("user_id"),
        "email": payload.get("email"),
        "display_name": payload.get("display_name"),
        "tenant_id": tenant_id,
        "org_role": payload.get("org_role"),
        "org": org,
    }
