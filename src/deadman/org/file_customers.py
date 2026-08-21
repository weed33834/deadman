"""机构客户/案件 文件降级实现（DATABASE_URL 为空时的私有化单机路径）

接口签名与 db/repositories.py 的 CustomerRepository / CaseRepository /
CaseEventRepository 完全一致（async 方法 + 同样的 dict 视图），
由 web/deps.py 的 get_customer_repo / get_case_repo / get_case_event_repo
按 db_enabled() 二选一分发。这样路由层零改动即可双轨运行。

存储布局（对齐 org/store.py 原子写模式）：
    {data_dir}/customers.json      {customer_id: {...}}
    {data_dir}/cases.json          {case_id: {...}}
    {data_dir}/case_events.json    {case_id: [event, ...]}

隔离：每条记录带 org_id 字段，查询一律双键过滤；事件按 case_id 索引。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..utils.jsonio import atomic_write_json
from .case_flow import validate_transition

_DEFAULT_DATA_DIR = Path.home() / ".deadman" / "org"

_CUSTOMER_EDITABLE = {
    "display_name",
    "province",
    "stage",
    "owner_user_id",
    "relationships",
    "tags",
}
_CASE_EDITABLE = {"case_type", "stage", "assignee_user_id", "priority", "source"}


def _now_iso() -> str:
    """UTC ISO 时间戳（与 DB 版 DateTime 对齐）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


class _FileStoreBase:
    """原子写 + 线程锁的通用基类。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir: Path = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.customers_file: Path = self.data_dir / "customers.json"
        self.cases_file: Path = self.data_dir / "cases.json"
        self.events_file: Path = self.data_dir / "case_events.json"
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _customers(self) -> dict[str, Any]:
        with self._lock:
            return _load(self.customers_file)

    def _cases(self) -> dict[str, Any]:
        with self._lock:
            return _load(self.cases_file)

    def _events(self) -> dict[str, Any]:
        with self._lock:
            return _load(self.events_file)


class CustomerRepository(_FileStoreBase):
    """客户 Repository（文件版）- 接口与 DB 版一致。"""

    async def list_by_org(self, org_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                c
                for c in self._customers().values()
                if c.get("org_id") == org_id
            ]
        rows.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return rows

    async def get(self, org_id: str, customer_id: str) -> dict[str, Any] | None:
        with self._lock:
            c = self._customers().get(customer_id)
            if c is not None and c.get("org_id") == org_id:
                return c
            return None

    async def count_by_org(self, org_id: str) -> int:
        return len(await self.list_by_org(org_id))

    async def create(
        self, org_id: str, data: dict[str, Any], actor_user_id: str | None = None
    ) -> dict[str, Any]:
        display_name = str(data.get("display_name", "")).strip()
        if not display_name:
            raise ValueError("display_name 不能为空")
        now = _now_iso()
        customer = {
            "id": _uid(),
            "org_id": org_id,
            "display_name": display_name,
            "province": str(data.get("province", "") or ""),
            "stage": str(data.get("stage", "planning") or "planning"),
            "owner_user_id": data.get("owner_user_id") or None,
            "relationships": list(data.get("relationships", []) or []),
            "tags": list(data.get("tags", []) or []),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            data_all = self._customers()
            data_all[customer["id"]] = customer
            _atomic_write(self.customers_file, data_all)
        return customer

    async def update(
        self, org_id: str, customer_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        fields = {k: v for k, v in data.items() if k in _CUSTOMER_EDITABLE}
        if not fields:
            return None
        with self._lock:
            all_data = self._customers()
            c = all_data.get(customer_id)
            if c is None or c.get("org_id") != org_id:
                return None
            for k, v in fields.items():
                if k == "display_name" and (not v or not str(v).strip()):
                    raise ValueError("display_name 不能为空")
                c[k] = v
            c["updated_at"] = _now_iso()
            all_data[customer_id] = c
            _atomic_write(self.customers_file, all_data)
            return c

    async def delete(self, org_id: str, customer_id: str) -> bool:
        with self._lock:
            all_data = self._customers()
            c = all_data.get(customer_id)
            if c is None or c.get("org_id") != org_id:
                return False
            del all_data[customer_id]
            _atomic_write(self.customers_file, all_data)
            return True


class CaseRepository(_FileStoreBase):
    """案件 Repository（文件版）- 接口与 DB 版一致。"""

    async def list_by_org(
        self, org_id: str, customer_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                c for c in self._cases().values() if c.get("org_id") == org_id
            ]
            if customer_id:
                rows = [c for c in rows if c.get("customer_id") == customer_id]
        rows.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return rows

    async def get(self, org_id: str, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            c = self._cases().get(case_id)
            if c is not None and c.get("org_id") == org_id:
                return c
            return None

    async def count_by_org(self, org_id: str, status: str | None = None) -> int:
        rows = await self.list_by_org(org_id)
        if status:
            rows = [c for c in rows if c.get("status") == status]
        return len(rows)

    async def create(
        self, org_id: str, data: dict[str, Any], actor_user_id: str
    ) -> dict[str, Any]:
        customer_id = str(data.get("customer_id", "")).strip()
        if not customer_id:
            raise ValueError("customer_id 不能为空")
        now = _now_iso()
        case = {
            "id": _uid(),
            "org_id": org_id,
            "customer_id": customer_id,
            "case_type": str(data.get("case_type", "funeral") or "funeral"),
            "status": str(data.get("status", "created") or "created"),
            "stage": str(data.get("stage", "") or ""),
            "assignee_user_id": data.get("assignee_user_id") or None,
            "priority": str(data.get("priority", "normal") or "normal"),
            "source": str(data.get("source", "manual") or "manual"),
            "closed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            all_data = self._cases()
            all_data[case["id"]] = case
            _atomic_write(self.cases_file, all_data)
            self._append_event(
                org_id,
                case["id"],
                actor_user_id,
                "case.create",
                {"case_type": case["case_type"], "customer_id": customer_id},
                now,
            )
        return case

    async def update(
        self, org_id: str, case_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        fields = {k: v for k, v in data.items() if k in _CASE_EDITABLE}
        if not fields:
            return None
        with self._lock:
            all_data = self._cases()
            c = all_data.get(case_id)
            if c is None or c.get("org_id") != org_id:
                return None
            for k, v in fields.items():
                c[k] = v
            c["updated_at"] = _now_iso()
            all_data[case_id] = c
            _atomic_write(self.cases_file, all_data)
            return c

    async def update_status(
        self, org_id: str, case_id: str, to_status: str, actor_user_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            all_data = self._cases()
            c = all_data.get(case_id)
            if c is None or c.get("org_id") != org_id:
                return None
            errors = validate_transition(c.get("status"), to_status)
            if errors:
                raise ValueError("; ".join(errors))
            from_status = c["status"]
            c["status"] = to_status
            c["updated_at"] = _now_iso()
            if to_status == "closed":
                c["closed_at"] = _now_iso()
            all_data[case_id] = c
            _atomic_write(self.cases_file, all_data)
            self._append_event(
                org_id, case_id, actor_user_id,
                "case.status_change", {"from": from_status, "to": to_status},
                c["updated_at"],
            )
            return c

    async def assign(
        self, org_id: str, case_id: str, assignee_user_id: str, actor_user_id: str
    ) -> dict[str, Any] | None:
        if not assignee_user_id:
            raise ValueError("assignee_user_id 不能为空")
        with self._lock:
            all_data = self._cases()
            c = all_data.get(case_id)
            if c is None or c.get("org_id") != org_id:
                return None
            prev = c.get("assignee_user_id")
            c["assignee_user_id"] = assignee_user_id
            c["updated_at"] = _now_iso()
            if c.get("status") == "created":
                c["status"] = "assigned"
            all_data[case_id] = c
            _atomic_write(self.cases_file, all_data)
            self._append_event(
                org_id, case_id, actor_user_id,
                "case.assign", {"from": prev, "to": assignee_user_id},
                c["updated_at"],
            )
            return c

    def _append_event(
        self, org_id: str, case_id: str, actor_user_id: str, action: str,
        detail: dict[str, Any], created_at: str,
    ) -> None:
        events = self._events()
        events.setdefault(case_id, []).append(
            {
                "id": _uid(),
                "org_id": org_id,
                "case_id": case_id,
                "actor_user_id": actor_user_id,
                "action": action,
                "detail": detail,
                "created_at": created_at,
            }
        )
        _atomic_write(self.events_file, events)


class CaseEventRepository(_FileStoreBase):
    """案件事件 Repository（文件版）- 只增不改。"""

    async def add(
        self,
        org_id: str,
        case_id: str,
        actor_user_id: str,
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            c = self._cases().get(case_id)
            if c is None or c.get("org_id") != org_id:
                raise ValueError("案件不存在或不属于该机构")
            events = self._events()
            now = _now_iso()
            event = {
                "id": _uid(),
                "org_id": org_id,
                "case_id": case_id,
                "actor_user_id": actor_user_id,
                "action": action,
                "detail": detail or {},
                "created_at": now,
            }
            events.setdefault(case_id, []).append(event)
            _atomic_write(self.events_file, events)
            return event

    async def list_by_case(self, org_id: str, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            events = self._events().get(case_id, [])
            rows = [e for e in events if e.get("org_id") == org_id]
        rows.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return rows
