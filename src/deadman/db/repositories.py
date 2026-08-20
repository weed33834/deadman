"""Repository 层 - DB↔文件双写抽象

设计：
    - BaseRepository: 提供 DB 会话获取 + 降级判断的通用基类
    - UserRepository: 用户 CRUD，DB 优先 / 文件回退的双写实现
    - CronJobRepository: Cron 任务（扩展④b 完善）
    - 双写策略：写操作 DB+文件同步（DB 主，文件备）；
      读操作 DB 优先，DB 未命中或未启用时回退文件存储。

与现有代码的关系：
    现有 auth/store.py UserStore 等保持不变（文件存储原样工作）。
    Repository 是上层封装：DB 启用时双写，未启用时纯转发给文件存储。
    这样实现零停机迁移：先部署代码（双写）→ 回填历史数据 → 切读 DB → 移除文件写。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..org.case_flow import validate_transition
from ..utils.db_retry import best_effort_db_write
from .engine import db_enabled, get_async_session_factory
from .models import Case, CaseEvent, Customer, User

logger = logging.getLogger(__name__)


# =====================================================================
# BaseRepository
# =====================================================================
class BaseRepository:
    """Repository 基类 - 提供会话管理与降级判断"""

    @staticmethod
    def _db_active(session: AsyncSession | None) -> bool:
        """判断当前是否会话是否可用于 DB 操作。"""
        return session is not None and db_enabled()

    @staticmethod
    def _new_session() -> AsyncSession | None:
        """获取一个新的异步会话（无 FastAPI 依赖注入时使用）。

        DATABASE_URL 未配置时返回 None。
        调用方需自行 `async with` 管理生命周期。
        """
        if not db_enabled():
            return None
        return get_async_session_factory()()

    @staticmethod
    def _to_dict(obj: Any) -> dict[str, Any]:
        """将 ORM 对象转为 dict（不含 SQLAlchemy 内部字段）。"""
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# =====================================================================
# UserRepository - 用户表双写
# =====================================================================
class UserRepository(BaseRepository):
    """用户 Repository - DB↔文件双写

    用法：
        repo = UserRepository(file_store=UserStore())
        user = await repo.create(email, password, display_name)  # 自动双写

    DB 未启用时，所有操作纯转发给 file_store（向后兼容）。
    """

    def __init__(self, file_store: Any | None = None) -> None:
        """
        Args:
            file_store: 现有 UserStore 实例（文件存储）。None 时仅用 DB。
        """
        self.file_store = file_store

    # ------------------------------------------------------------------
    # 创建用户
    # ------------------------------------------------------------------
    async def create(
        self, email: str, password: str, display_name: str | None = None
    ) -> dict[str, Any]:
        """创建用户 - 双写（文件存储负责密码哈希，DB 同步元数据）。

        策略：先写文件存储（含密码哈希逻辑），成功后同步到 DB。
        DB 写失败仅记日志不阻断（文件存储是 source of truth during migration）。
        """
        # 1. 文件存储创建（含密码哈希、email_hmac、唯一性校验）
        if self.file_store is not None:
            result = self.file_store.register(email, password, display_name)
            user_id = result["user_id"]
        else:
            # 纯 DB 模式（无文件回退）- 需自行哈希
            result = await self._create_db_only(email, password, display_name)
            return result

        # 2. DB 同步（best-effort，失败不阻断文件存储结果）
        await self._sync_user_to_db(user_id, result)
        return result

    async def _sync_user_to_db(self, user_id: str, public_view: dict[str, Any]) -> None:
        """将文件存储的用户记录同步到 DB（best-effort）。"""
        if not db_enabled():
            return

        async def _op() -> None:
            # 从文件存储读取完整记录（含 password_hash/salt）
            full_record = self.file_store.get_user_raw(user_id) if self.file_store else None
            if full_record is None:
                logger.warning("同步用户到 DB 失败：文件存储无完整记录 user=%s", user_id)
                return

            async with get_async_session_factory()() as session:
                existing = await session.get(User, user_id)
                if existing is not None:
                    return  # 已存在，跳过
                created_at = _parse_dt(full_record.get("created_at"))
                user = User(
                    user_id=user_id,
                    email=full_record.get("email", ""),
                    email_hmac=full_record.get("email_hmac", ""),
                    password_hash=full_record.get("password_hash", ""),
                    salt=full_record.get("salt", ""),
                    role=full_record.get("role", "user"),
                    family_id=full_record.get("family_id"),
                    display_name=full_record.get("display_name", ""),
                    password_updated_at=_parse_dt(full_record.get("password_updated_at")),
                    created_at=created_at or datetime.utcnow(),
                    updated_at=created_at or datetime.utcnow(),
                )
                session.add(user)
                await session.commit()

        await best_effort_db_write(_op, f"同步用户到 DB（user={user_id}）", logger)

    async def _create_db_only(
        self, email: str, password: str, display_name: str | None
    ) -> dict[str, Any]:
        """纯 DB 模式创建用户（无文件回退时）。"""
        import hashlib
        import hmac
        import os
        import uuid

        email_normalized = email.strip().lower()
        salt = os.urandom(16)
        # PBKDF2-HMAC-SHA256, 100k iterations（与 UserStore 一致）
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000, 32)
        server_secret = os.getenv("DEADMAN_JWT_SECRET", "deadman-default-secret").encode()
        email_hmac = hmac.new(server_secret, email_normalized.encode(), hashlib.sha256).hexdigest()
        user_id = str(uuid.uuid4())
        now = datetime.utcnow()

        async with get_async_session_factory()() as session:
            user = User(
                user_id=user_id,
                email=email_normalized,
                email_hmac=email_hmac,
                password_hash=password_hash.hex(),
                salt=salt.hex(),
                role="user",
                display_name=display_name or email_normalized.split("@")[0],
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.commit()
        return {
            "user_id": user_id,
            "email": email_normalized,
            "display_name": display_name or email_normalized.split("@")[0],
            "created_at": now.isoformat(),
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        """按 user_id 查询 - DB 优先，回退文件存储。"""
        if db_enabled():
            try:
                async with get_async_session_factory()() as session:
                    user = await session.get(User, user_id)
                    if user is not None:
                        return _user_to_public_view(user)
            except Exception as exc:
                logger.warning("DB 查询用户失败，回退文件存储 user=%s: %s", user_id, exc)
        # 回退文件存储
        if self.file_store is not None:
            return self.file_store.get_user(user_id)
        return None

    async def get_by_email_hmac(self, email_hmac: str) -> dict[str, Any] | None:
        """按 email_hmac 查询（登录验证用）- DB 优先（O(1) 索引）。"""
        if db_enabled():
            try:
                async with get_async_session_factory()() as session:
                    stmt = select(User).where(User.email_hmac == email_hmac)
                    user = (await session.execute(stmt)).scalar_one_or_none()
                    if user is not None:
                        return _user_to_public_view(user)
            except Exception as exc:
                logger.warning("DB 按 email_hmac 查询失败: %s", exc)
        return None

    async def count(self) -> int:
        """用户总数 - DB 优先。"""
        if db_enabled():
            try:
                from sqlalchemy import func

                async with get_async_session_factory()() as session:
                    stmt = select(func.count()).select_from(User)
                    return int((await session.execute(stmt)).scalar() or 0)
            except Exception as exc:
                logger.warning("DB 用户计数失败: %s", exc)
        if self.file_store is not None:
            return len(self.file_store._load())
        return 0


# =====================================================================
# CustomerRepository - 机构客户档案（B2B-IMPLEMENTATION Step 5）
# =====================================================================
class CustomerRepository(BaseRepository):
    """客户 Repository - 按 org_id 过滤，get 双键校验越权。

    用法（无 FastAPI 依赖时）：
        repo = CustomerRepository()
        rows = await repo.list_by_org(org_id)
        c = await repo.get(org_id, customer_id)   # 跨机构 id 返回 None
    """

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def list_by_org(self, org_id: str) -> list[dict[str, Any]]:
        """按机构列出客户（含主办人过滤可选）。"""
        async with get_async_session_factory()() as session:
            stmt = (
                select(Customer)
                .where(Customer.org_id == org_id)
                .order_by(Customer.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_customer_to_dict(r) for r in rows]

    async def get(self, org_id: str, customer_id: str) -> dict[str, Any] | None:
        """双键定位：org_id + id 同时匹配，跨机构 id 返回 None。"""
        async with get_async_session_factory()() as session:
            stmt = select(Customer).where(
                Customer.org_id == org_id, Customer.id == customer_id
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _customer_to_dict(row) if row else None

    async def count_by_org(self, org_id: str) -> int:
        """机构客户总数（仪表盘聚合）。"""
        from sqlalchemy import func

        async with get_async_session_factory()() as session:
            stmt = (
                select(func.count())
                .select_from(Customer)
                .where(Customer.org_id == org_id)
            )
            return int((await session.execute(stmt)).scalar() or 0)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def create(
        self, org_id: str, data: dict[str, Any], actor_user_id: str | None = None
    ) -> dict[str, Any]:
        """创建客户；display_name 必填。"""
        display_name = str(data.get("display_name", "")).strip()
        if not display_name:
            raise ValueError("display_name 不能为空")
        now = datetime.now()
        customer = Customer(
            id=str(uuid.uuid4()),
            org_id=org_id,
            display_name=display_name,
            province=str(data.get("province", "") or ""),
            stage=str(data.get("stage", "planning") or "planning"),
            owner_user_id=data.get("owner_user_id") or None,
            relationships=list(data.get("relationships", []) or []),
            tags=list(data.get("tags", []) or []),
            created_at=now,
            updated_at=now,
        )
        async with get_async_session_factory()() as session:
            session.add(customer)
            await session.commit()
        return _customer_to_dict(customer)

    async def update(
        self, org_id: str, customer_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """更新客户（白名单字段）；跨机构返回 None。"""
        allowed = {
            "display_name",
            "province",
            "stage",
            "owner_user_id",
            "relationships",
            "tags",
        }
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return None
        async with get_async_session_factory()() as session:
            stmt = select(Customer).where(
                Customer.org_id == org_id, Customer.id == customer_id
            )
            customer = (await session.execute(stmt)).scalar_one_or_none()
            if customer is None:
                return None
            for k, v in fields.items():
                if k == "display_name" and (not v or not str(v).strip()):
                    raise ValueError("display_name 不能为空")
                setattr(customer, k, v)
            customer.updated_at = datetime.now()
            await session.commit()
            return _customer_to_dict(customer)

    async def delete(self, org_id: str, customer_id: str) -> bool:
        """删除客户（org_admin 限定）；跨机构返回 False。"""
        async with get_async_session_factory()() as session:
            stmt = select(Customer).where(
                Customer.org_id == org_id, Customer.id == customer_id
            )
            customer = (await session.execute(stmt)).scalar_one_or_none()
            if customer is None:
                return False
            await session.delete(customer)
            await session.commit()
            return True


# =====================================================================
# CaseRepository / CaseEventRepository - 案件 + 事件（审计）
# =====================================================================
class CaseRepository(BaseRepository):
    """案件 Repository - 状态变更强制落 case_events。

    create/update_status 内部自动写事件；assign 也写事件。
    """

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def list_by_org(
        self, org_id: str, customer_id: str | None = None
    ) -> list[dict[str, Any]]:
        """按机构列出案件（可按客户过滤）。"""
        async with get_async_session_factory()() as session:
            stmt = select(Case).where(Case.org_id == org_id)
            if customer_id:
                stmt = stmt.where(Case.customer_id == customer_id)
            stmt = stmt.order_by(Case.created_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
            return [_case_to_dict(r) for r in rows]

    async def get(self, org_id: str, case_id: str) -> dict[str, Any] | None:
        """双键定位：org_id + id 同时匹配，跨机构 id 返回 None。"""
        async with get_async_session_factory()() as session:
            stmt = select(Case).where(Case.org_id == org_id, Case.id == case_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _case_to_dict(row) if row else None

    async def count_by_org(self, org_id: str, status: str | None = None) -> int:
        """机构案件计数（可按状态过滤，仪表盘聚合）。"""
        from sqlalchemy import func

        async with get_async_session_factory()() as session:
            stmt = select(func.count()).select_from(Case).where(Case.org_id == org_id)
            if status:
                stmt = stmt.where(Case.status == status)
            return int((await session.execute(stmt)).scalar() or 0)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def create(
        self,
        org_id: str,
        data: dict[str, Any],
        actor_user_id: str,
    ) -> dict[str, Any]:
        """创建案件；customer_id / case_type 必填；自动落 case.create 事件。"""
        customer_id = str(data.get("customer_id", "")).strip()
        case_type = str(data.get("case_type", "funeral") or "funeral")
        if not customer_id:
            raise ValueError("customer_id 不能为空")
        now = datetime.now()
        case = Case(
            id=str(uuid.uuid4()),
            org_id=org_id,
            customer_id=customer_id,
            case_type=case_type,
            status=str(data.get("status", "created") or "created"),
            stage=str(data.get("stage", "") or ""),
            assignee_user_id=data.get("assignee_user_id") or None,
            priority=str(data.get("priority", "normal") or "normal"),
            source=str(data.get("source", "manual") or "manual"),
            closed_at=None,
            created_at=now,
            updated_at=now,
        )
        async with get_async_session_factory()() as session:
            session.add(case)
            session.add(
                _new_event(
                    org_id, case.id, actor_user_id,
                    "case.create", {"case_type": case_type, "customer_id": customer_id},
                    now,
                )
            )
            await session.commit()
        return _case_to_dict(case)

    async def update(
        self, org_id: str, case_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """更新案件（白名单字段）；跨机构返回 None。

        注意：status 变更请走 update_status()，本方法忽略 status 字段。
        """
        allowed = {"case_type", "stage", "assignee_user_id", "priority", "source"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return None
        async with get_async_session_factory()() as session:
            stmt = select(Case).where(Case.org_id == org_id, Case.id == case_id)
            case = (await session.execute(stmt)).scalar_one_or_none()
            if case is None:
                return None
            for k, v in fields.items():
                setattr(case, k, v)
            case.updated_at = datetime.now()
            await session.commit()
            return _case_to_dict(case)

    async def update_status(
        self,
        org_id: str,
        case_id: str,
        to_status: str,
        actor_user_id: str,
    ) -> dict[str, Any] | None:
        """状态机流转：校验 CASE_FLOW，非法迁移抛 ValueError；合法则落事件。

        跨机构返回 None（不存在）。
        """
        async with get_async_session_factory()() as session:
            stmt = select(Case).where(Case.org_id == org_id, Case.id == case_id)
            case = (await session.execute(stmt)).scalar_one_or_none()
            if case is None:
                return None
            errors = validate_transition(case.status, to_status)
            if errors:
                raise ValueError("; ".join(errors))
            from_status = case.status
            case.status = to_status
            case.updated_at = datetime.now()
            if to_status == "closed":
                case.closed_at = datetime.now()
            session.add(
                _new_event(
                    org_id, case.id, actor_user_id,
                    "case.status_change",
                    {"from": from_status, "to": to_status},
                    datetime.now(),
                )
            )
            await session.commit()
            return _case_to_dict(case)

    async def assign(
        self, org_id: str, case_id: str, assignee_user_id: str, actor_user_id: str
    ) -> dict[str, Any] | None:
        """分配案件给成员；写 case.assign 事件。跨机构返回 None。"""
        if not assignee_user_id:
            raise ValueError("assignee_user_id 不能为空")
        async with get_async_session_factory()() as session:
            stmt = select(Case).where(Case.org_id == org_id, Case.id == case_id)
            case = (await session.execute(stmt)).scalar_one_or_none()
            if case is None:
                return None
            prev = case.assignee_user_id
            case.assignee_user_id = assignee_user_id
            case.updated_at = datetime.now()
            # 分配后默认进入 assigned（若当前仍是 created）
            if case.status == "created":
                case.status = "assigned"
            session.add(
                _new_event(
                    org_id, case.id, actor_user_id,
                    "case.assign",
                    {"from": prev, "to": assignee_user_id},
                    datetime.now(),
                )
            )
            await session.commit()
            return _case_to_dict(case)


class CaseEventRepository(BaseRepository):
    """案件事件 Repository - 只增不改（审计）。"""

    async def add(
        self,
        org_id: str,
        case_id: str,
        actor_user_id: str,
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加事件（先校验案件归属 org）。"""
        now = datetime.now()
        async with get_async_session_factory()() as session:
            stmt = select(Case).where(Case.org_id == org_id, Case.id == case_id)
            case = (await session.execute(stmt)).scalar_one_or_none()
            if case is None:
                raise ValueError("案件不存在或不属于该机构")
            event = _new_event(
                org_id, case_id, actor_user_id, action, detail or {}, now
            )
            session.add(event)
            await session.commit()
            return _event_to_dict(event)

    async def list_by_case(self, org_id: str, case_id: str) -> list[dict[str, Any]]:
        """案件时间线（倒序，最新在前）；跨机构返回空列表。"""
        async with get_async_session_factory()() as session:
            stmt = (
                select(CaseEvent)
                .where(CaseEvent.org_id == org_id, CaseEvent.case_id == case_id)
                .order_by(CaseEvent.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_event_to_dict(r) for r in rows]


# =====================================================================
# 辅助函数
# =====================================================================
def _parse_dt(v: Any) -> datetime | None:
    """解析 ISO 格式 datetime 字符串（兼容文件存储格式）。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


def _user_to_public_view(user: User) -> dict[str, Any]:
    """ORM User → 公开视图 dict（不含 password_hash/salt）。"""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "family_id": user.family_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _dt_iso(v: datetime | None) -> str | None:
    """datetime → ISO 字符串（None 保持 None，与文件版一致）。"""
    return v.isoformat() if v else None


def _customer_to_dict(c: Customer) -> dict[str, Any]:
    return {
        "id": c.id,
        "org_id": c.org_id,
        "display_name": c.display_name,
        "province": c.province,
        "stage": c.stage,
        "owner_user_id": c.owner_user_id,
        "relationships": list(c.relationships or []),
        "tags": list(c.tags or []),
        "created_at": _dt_iso(c.created_at),
        "updated_at": _dt_iso(c.updated_at),
    }


def _case_to_dict(c: Case) -> dict[str, Any]:
    return {
        "id": c.id,
        "org_id": c.org_id,
        "customer_id": c.customer_id,
        "case_type": c.case_type,
        "status": c.status,
        "stage": c.stage,
        "assignee_user_id": c.assignee_user_id,
        "priority": c.priority,
        "source": c.source,
        "closed_at": _dt_iso(c.closed_at),
        "created_at": _dt_iso(c.created_at),
        "updated_at": _dt_iso(c.updated_at),
    }


def _event_to_dict(e: CaseEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "org_id": e.org_id,
        "case_id": e.case_id,
        "actor_user_id": e.actor_user_id,
        "action": e.action,
        "detail": dict(e.detail or {}),
        "created_at": _dt_iso(e.created_at),
    }


def _new_event(
    org_id: str,
    case_id: str,
    actor_user_id: str,
    action: str,
    detail: dict[str, Any],
    now: datetime,
) -> CaseEvent:
    """构造 CaseEvent 实例（供 CaseRepository / CaseEventRepository 复用）。"""
    return CaseEvent(
        id=str(uuid.uuid4()),
        org_id=org_id,
        case_id=case_id,
        actor_user_id=actor_user_id,
        action=action,
        detail=detail,
        created_at=now,
    )


__all__ = [
    "BaseRepository",
    "UserRepository",
    "CustomerRepository",
    "CaseRepository",
    "CaseEventRepository",
]
