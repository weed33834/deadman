"""deadman.org - 机构域（To B）

机构 = 租户：独立数据空间 / 成员 / 套餐；业务数据归属机构，不归属个人员工。

模块：
  - models.Organization / Membership：数据模型
  - store.OrgStore：机构与成员 JSON 存储
  - invites.InviteStore：成员邀请令牌（单次使用 + TTL）
  - rbac：机构内角色等级与能力矩阵（can/rank/require_rank）

对齐：B2B-TECH-DESIGN §2-3 / B2B-IMPLEMENTATION Step 2
"""

from .invites import InviteStore
from .models import Membership, Organization
from .rbac import ORG_ROLES, can, rank, require_rank
from .store import OrgStore

__all__ = [
    "OrgStore",
    "InviteStore",
    "Organization",
    "Membership",
    "ORG_ROLES",
    "can",
    "rank",
    "require_rank",
]
