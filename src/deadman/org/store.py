"""OrgStore - 机构与成员的 JSON 存储

设计（对齐 auth/store.py 模式）:
  - orgs.json:      {org_id: Organization.to_dict()}
  - memberships.json: {f"{org_id}:{user_id}": Membership.to_dict()}
  - 原子写（.tmp + os.replace）+ 线程锁
  - slug 唯一；跨租户数据隔离由 TenantContext 强制（本 store 只负责归属关系）

存储目录：settings.org_data_dir（默认 ~/.deadman/org）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .models import ORG_PLANS, ORG_STATUS, Membership, Organization
from .rbac import ORG_ROLES

_DEFAULT_DATA_DIR = Path.home() / ".deadman" / "org"

# 可被 update_org 更新的字段白名单
_ORG_EDITABLE = {
    "name",
    "industry_template",
    "status",
    "plan",
    "features",
    "quotas",
    "expires_at",
}


class OrgStore:
    """机构与成员存储。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir: Path = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.orgs_file: Path = self.data_dir / "orgs.json"
        self.members_file: Path = self.data_dir / "memberships.json"
        self.invites_file: Path = self.data_dir / "invites.json"
        self._lock = threading.RLock()

    # ================================================================
    # 机构
    # ================================================================

    def create_org(
        self,
        name: str,
        slug: str,
        industry_template: str = "funeral",
        plan: str = "free",
        features: list[str] | None = None,
        quotas: dict[str, Any] | None = None,
    ) -> Organization:
        """创建机构；slug 必须唯一（用于客户门户域名/检索）。"""
        if not name or not isinstance(name, str):
            raise ValueError("机构名称不能为空")
        slug_norm = (slug or "").strip().lower()
        if not slug_norm:
            raise ValueError("机构 slug 不能为空")
        if not any(ch.isalnum() for ch in slug_norm):
            raise ValueError("机构 slug 必须包含字母或数字")
        org = Organization.create(
            name=name,
            slug=slug_norm,
            industry_template=industry_template,
            plan=plan,
            features=features,
            quotas=quotas,
        )
        with self._lock:
            data = self._load(self.orgs_file)
            if any(o.get("slug") == slug_norm for o in data.values()):
                raise ValueError(f"机构 slug 已存在: {slug_norm}")
            data[org.org_id] = org.to_dict()
            self._atomic_write(self.orgs_file, data)
        return org

    def get_org(self, org_id: str) -> Organization | None:
        with self._lock:
            data = self._load(self.orgs_file)
            record = data.get(org_id)
            return Organization.from_dict(record) if record else None

    def get_org_by_slug(self, slug: str) -> Organization | None:
        if not slug:
            return None
        slug_norm = slug.strip().lower()
        with self._lock:
            for _org_id, record in self._load(self.orgs_file).items():
                if record.get("slug") == slug_norm:
                    return Organization.from_dict(record)
        return None

    def update_org(self, org_id: str, **fields: Any) -> Organization | None:
        """更新机构字段（白名单）。"""
        allowed = {k: v for k, v in fields.items() if k in _ORG_EDITABLE}
        if not allowed:
            return None
        with self._lock:
            data = self._load(self.orgs_file)
            record = data.get(org_id)
            if record is None:
                return None
            for k, v in allowed.items():
                if k == "name" and (not v or not isinstance(v, str)):
                    raise ValueError("机构名称不能为空")
                if k == "status" and v not in ORG_STATUS:
                    raise ValueError(f"status 仅支持 {ORG_STATUS}")
                if k == "plan" and v not in ORG_PLANS:
                    raise ValueError(f"plan 仅支持 {ORG_PLANS}")
                record[k] = v
            data[org_id] = record
            self._atomic_write(self.orgs_file, data)
            return Organization.from_dict(record)

    def list_orgs(self) -> list[Organization]:
        with self._lock:
            return [Organization.from_dict(r) for r in self._load(self.orgs_file).values()]

    def delete_org(self, org_id: str) -> bool:
        """删除机构（含成员关系；业务数据保留待手动清理）。"""
        with self._lock:
            data = self._load(self.orgs_file)
            if org_id not in data:
                return False
            del data[org_id]
            self._atomic_write(self.orgs_file, data)
            members = self._load(self.members_file)
            for k in [k for k in members if k.startswith(f"{org_id}:")]:
                del members[k]
            self._atomic_write(self.members_file, members)
            # 清理该机构残留的邀请令牌，防止删除后被消费
            invites = self._load(self.invites_file)
            stale = [t for t, e in invites.items() if e.get("org_id") == org_id]
            for t in stale:
                del invites[t]
            if stale:
                self._atomic_write(self.invites_file, invites)
            return True

    # ================================================================
    # 成员
    # ================================================================

    def add_member(
        self,
        org_id: str,
        user_id: str,
        org_role: str = "viewer",
        invited_by: str | None = None,
    ) -> Membership:
        """添加成员；机构必须存在；重复添加为幂等（更新角色/状态为 active）。"""
        if org_role not in ORG_ROLES:
            raise ValueError(f"org_role 仅支持 {ORG_ROLES}")
        if not user_id:
            raise ValueError("user_id 不能为空")
        with self._lock:
            orgs = self._load(self.orgs_file)
            if org_id not in orgs:
                raise ValueError(f"机构不存在: {org_id}")
            members = self._load(self.members_file)
            key = f"{org_id}:{user_id}"
            existing = members.get(key)
            if existing:
                existing["org_role"] = org_role
                existing["status"] = "active"
                if invited_by:
                    existing["invited_by"] = invited_by
                members[key] = existing
                self._atomic_write(self.members_file, members)
                return Membership.from_dict(existing)

            member = Membership(
                org_id=org_id,
                user_id=user_id,
                org_role=org_role,
                status="active",
                invited_by=invited_by,
                joined_at=time.time(),
            )
            members[key] = member.to_dict()
            self._atomic_write(self.members_file, members)
            return member

    def get_membership(self, org_id: str, user_id: str) -> Membership | None:
        with self._lock:
            data = self._load(self.members_file)
            record = data.get(f"{org_id}:{user_id}")
            return Membership.from_dict(record) if record else None

    def list_members(self, org_id: str) -> list[Membership]:
        prefix = f"{org_id}:"
        with self._lock:
            return [
                Membership.from_dict(r)
                for k, r in self._load(self.members_file).items()
                if k.startswith(prefix)
            ]

    def set_member_role(self, org_id: str, user_id: str, org_role: str) -> Membership | None:
        if org_role not in ORG_ROLES:
            raise ValueError(f"org_role 仅支持 {ORG_ROLES}")
        with self._lock:
            data = self._load(self.members_file)
            key = f"{org_id}:{user_id}"
            record = data.get(key)
            if record is None:
                return None
            record["org_role"] = org_role
            data[key] = record
            self._atomic_write(self.members_file, data)
            return Membership.from_dict(record)

    def set_member_status(self, org_id: str, user_id: str, status: str) -> Membership | None:
        if status not in ("active", "disabled"):
            raise ValueError("status 仅支持 active/disabled")
        with self._lock:
            data = self._load(self.members_file)
            key = f"{org_id}:{user_id}"
            record = data.get(key)
            if record is None:
                return None
            record["status"] = status
            data[key] = record
            self._atomic_write(self.members_file, data)
            return Membership.from_dict(record)

    def remove_member(self, org_id: str, user_id: str) -> bool:
        with self._lock:
            data = self._load(self.members_file)
            key = f"{org_id}:{user_id}"
            if key not in data:
                return False
            del data[key]
            self._atomic_write(self.members_file, data)
            return True

    def list_user_orgs(self, user_id: str) -> list[Membership]:
        with self._lock:
            return [
                Membership.from_dict(r)
                for k, r in self._load(self.members_file).items()
                if k.endswith(f":{user_id}")
            ]

    # ================================================================
    # 内部工具
    # ================================================================

    def _load(self, path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _atomic_write(self, path: Path, data: dict[str, dict[str, Any]]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
