"""机构内 RBAC - 角色等级 + 能力矩阵

设计（对齐 B2B-PRODUCT-DESIGN §5.3 / B2B-TECH-DESIGN §5.3）:
  - 角色等级：viewer(0) < consultant(1) < case_manager(2) < org_admin(3)
  - 每个动作要求最低等级；跨租户数据访问由 TenantContext / 依赖层强制，
    本模块只做「机构内角色 → 能力」判定，不碰数据。
  - 纯函数，无 IO，可单测。
"""

from __future__ import annotations

from collections.abc import Callable

# 角色 → 等级（数值越大权限越高）
ROLE_RANK: dict[str, int] = {
    "viewer": 0,
    "consultant": 1,
    "case_manager": 2,
    "org_admin": 3,
}

# 动作 → 最低等级（与 B2B-TECH-DESIGN §5.3 权限矩阵一致）
MIN_RANK_FOR_ACTION: dict[str, int] = {
    # 只读（仪表盘 / 客户 / 案件查看）
    "org.view": 0,
    # 生成材料包 / 通知信函
    "org.material.generate": 1,
    # 客户 CRUD / 案件创建 / 分配 / 推进
    "org.case.manage": 2,
    # 编辑机构私有知识库
    "org.kb.edit": 2,
    # 成员管理（邀请 / 改角色 / 禁用）
    "org.members.manage": 3,
    # 查看审计日志
    "org.audit.view": 3,
    # 数据导出
    "org.export": 3,
    # 机构资料 / 套餐 / 授权码
    "org.settings.manage": 3,
}

ORG_ROLES = tuple(ROLE_RANK.keys())


def rank(role: str | None) -> int:
    """角色等级；未知角色视为 0（安全兜底）。"""
    if not role:
        return 0
    return ROLE_RANK.get(role, 0)


def can(role: str | None, action: str) -> bool:
    """角色是否允许某动作；未知动作视为拒绝（安全兜底）。"""
    required = MIN_RANK_FOR_ACTION.get(action)
    if required is None:
        return False
    return rank(role) >= required


def require_rank(min_role: str) -> Callable[[str | None], bool]:
    """构造「角色等级 >= 指定角色」的判定函数（供依赖/校验复用）。

    Args:
        min_role: 最低角色（viewer/consultant/case_manager/org_admin）

    Returns:
        predicate(role) -> bool
    """

    def _check(role: str | None) -> bool:
        return rank(role) >= rank(min_role)

    return _check
