"""机构域数据模型 - Organization / Membership（To B）

设计（对齐 B2B-TECH-DESIGN §3.1-3.2）:
  - Organization = 租户，独立数据空间 / 成员 / 套餐
  - Membership = 机构与用户的关联（机构内角色）；一个用户可属于多个机构
  - 业务数据归属机构，不归属个人员工

纯数据类，无 IO；持久化见 store.py。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 机构状态机
ORG_STATUS = ("active", "suspended", "expired")
# 套餐
ORG_PLANS = ("free", "pro", "enterprise")
# 机构内角色等级定义见 rbac.py（此处不重复，避免两处漂移）


@dataclass
class Organization:
    """机构 = 租户。"""

    org_id: str
    name: str
    slug: str
    industry_template: str = "funeral"  # 行业模板：funeral/insurance/estate/...
    status: str = "active"  # active|suspended|expired
    plan: str = "free"  # free|pro|enterprise
    features: list[str] = field(default_factory=list)  # 模块开关白名单
    quotas: dict[str, Any] = field(default_factory=dict)  # token/存储/工具配额
    created_at: float = 0.0
    expires_at: float | None = None

    @classmethod
    def create(
        cls,
        name: str,
        slug: str,
        industry_template: str = "funeral",
        plan: str = "free",
        features: list[str] | None = None,
        quotas: dict[str, Any] | None = None,
    ) -> Organization:
        """创建机构（生成 org_id / created_at）。"""
        return cls(
            org_id=str(uuid.uuid4()),
            name=name,
            slug=slug,
            industry_template=industry_template,
            plan=plan,
            features=list(features or []),
            quotas=dict(quotas or {}),
            created_at=time.time(),
            expires_at=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "name": self.name,
            "slug": self.slug,
            "industry_template": self.industry_template,
            "status": self.status,
            "plan": self.plan,
            "features": self.features or [],
            "quotas": self.quotas or {},
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Organization:
        return cls(
            org_id=data.get("org_id", ""),
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            industry_template=data.get("industry_template", "funeral"),
            status=data.get("status", "active"),
            plan=data.get("plan", "free"),
            features=data.get("features") or [],
            quotas=data.get("quotas") or {},
            created_at=data.get("created_at", 0.0),
            expires_at=data.get("expires_at"),
        )

    def is_active(self) -> bool:
        """机构是否可用：状态 active 且未过期。"""
        return self.status == "active" and (
            self.expires_at is None or time.time() <= self.expires_at
        )


@dataclass
class Membership:
    """机构-用户关联（机构内角色）。"""

    org_id: str
    user_id: str
    org_role: str = "viewer"  # org_admin|case_manager|consultant|viewer
    status: str = "active"  # active|disabled
    invited_by: str | None = None
    joined_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "user_id": self.user_id,
            "org_role": self.org_role,
            "status": self.status,
            "invited_by": self.invited_by,
            "joined_at": self.joined_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Membership:
        return cls(
            org_id=data.get("org_id", ""),
            user_id=data.get("user_id", ""),
            org_role=data.get("org_role", "viewer"),
            status=data.get("status", "active"),
            invited_by=data.get("invited_by"),
            joined_at=data.get("joined_at", 0.0),
        )

    def is_active(self) -> bool:
        return self.status == "active"
