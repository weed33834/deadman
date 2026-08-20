"""P7.3 多租户隔离 - TenantContext + 数据路径隔离 + JWT claim。

设计:
    - 每个租户有独立 tenant_id(UUID),所有数据按 tenant_id 分目录
    - JWT 加 tenant_id claim(由 auth/jwt.py 校验时填充)
    - 数据隔离三层:
        1. 路径层:~/.deadman/tenants/<tenant_id>/memory|vault|data
        2. 配额层:每租户独立配额(参见 quota.py)
        3. 运行时:ThreadLocal/ContextVar 携带 tenant_id,所有 IO 自动路由

    - 默认租户 default_tenant_id="default"(单租户场景平滑迁移)
    - feature flag 关闭时所有路径退回 ~/.deadman/,行为完全不变

租户模式 `DEADMAN_TENANT_MODE`:
    - single(默认):单机/C 端/私有化,所有路径走 ~/.deadman/,向后兼容零迁移
    - multi:SaaS 多租户,所有路径强制走 ~/.deadman/tenants/<tid>/

兼容旧配置:multi 模式显式开启;single 模式下仍可凭 feature flag 兜底。
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .feature_flags import is_enabled

logger = logging.getLogger(__name__)


# 默认租户 ID(单租户场景平滑迁移,所有数据落到 ~/.deadman/)
DEFAULT_TENANT_ID = os.environ.get("DEADMAN_DEFAULT_TENANT_ID", "default")

# 租户模式:single=单机/C 端/私有化(路径走 ~/.deadman/);multi=SaaS 多租户(路径走 tenants/<tid>/)
TENANT_MODE: str = os.environ.get("DEADMAN_TENANT_MODE", "single")

# 租户数据根目录
TENANTS_ROOT = Path(
    os.environ.get("DEADMAN_TENANTS_ROOT", str(Path.home() / ".deadman" / "tenants"))
)


@dataclass
class TenantInfo:
    """租户元数据。"""

    tenant_id: str
    name: str = ""
    plan: str = "free"  # free / pro / enterprise
    created_at: float = 0.0
    quota_token_per_day: int = 100_000
    quota_tool_calls_per_day: int = 1_000
    quota_storage_mb: int = 100
    features: list[str] = field(default_factory=list)  # 启用的 feature flag 白名单

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "plan": self.plan,
            "created_at": self.created_at,
            "quota_token_per_day": self.quota_token_per_day,
            "quota_tool_calls_per_day": self.quota_tool_calls_per_day,
            "quota_storage_mb": self.quota_storage_mb,
            "features": self.features or [],
        }


# ContextVar - 协程安全(支持 async + 线程)
_current_tenant: contextvars.ContextVar[TenantInfo | None] = contextvars.ContextVar(
    "current_tenant", default=None
)


class TenantContext:
    """租户上下文管理器。

    用法:
        with TenantContext(tenant):
            # 此作用域内所有 IO 自动路由到该租户目录
            memory.load()  # → /tenants/<tenant_id>/memory
    """

    def __init__(self, tenant: TenantInfo) -> None:
        self.tenant = tenant
        self._token: contextvars.Token | None = None

    def __enter__(self) -> TenantInfo:
        self._token = _current_tenant.set(self.tenant)
        return self.tenant

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token is not None:
            _current_tenant.reset(self._token)


def get_current_tenant() -> TenantInfo | None:
    """获取当前协程/线程的租户(None = 未设置,走默认)。"""
    return _current_tenant.get()


def get_current_tenant_id() -> str:
    """获取当前租户 ID(未设置时返回 default)。"""
    tenant = _current_tenant.get()
    return tenant.tenant_id if tenant else DEFAULT_TENANT_ID


def is_multi_tenant_enabled() -> bool:
    """多租户是否启用:multi 模式强制启用;否则回退 feature flag(向后兼容)。"""
    if TENANT_MODE == "multi":
        return True
    return is_enabled("multi_tenant")


# =====================================================================
# 路径路由
# =====================================================================


def resolve_tenant_path(sub_path: str, tenant_id: str | None = None, strict: bool = False) -> Path:
    """按租户路由数据路径。

    Args:
        sub_path: 业务路径(如 "memory/USER.md")
        tenant_id: 显式指定租户(优先于 ContextVar)
        strict: 单测钩子——multi 模式且既无显式 tenant_id 也无 TenantContext
            时抛 RuntimeError（防止静默落到 default 租户造成数据串扰）。

    Returns:
        - 多租户启用:~/.deadman/tenants/<tenant_id>/<sub_path>
        - 多租户关闭:~/.deadman/<sub_path>(平滑兼容)

    Raises:
        RuntimeError: strict=True 且 multi 模式下缺少租户上下文。
    """
    tid = tenant_id or get_current_tenant_id()
    if strict and is_multi_tenant_enabled() and not tenant_id and get_current_tenant() is None:
        raise RuntimeError(
            "多租户模式下缺少 TenantContext：无法确定数据归属租户，"
            "请通过 TenantMiddleware / TenantContext 绑定租户后再写入。"
        )
    if not is_multi_tenant_enabled():
        # 关闭:数据落到 ~/.deadman/<sub_path>(向后兼容)
        return Path.home() / ".deadman" / sub_path
    # 启用:数据落到 ~/.deadman/tenants/<tenant_id>/<sub_path>
    return TENANTS_ROOT / tid / sub_path


def resolve_memory_path(filename: str, tenant_id: str | None = None) -> Path:
    """便捷方法:解析 memory 目录下的文件。"""
    return resolve_tenant_path(f"memory/{filename}", tenant_id)


def resolve_vault_path(filename: str, tenant_id: str | None = None) -> Path:
    """便捷方法:解析 vault 目录下的文件。"""
    return resolve_tenant_path(f"vault/{filename}", tenant_id)


def resolve_data_path(filename: str, tenant_id: str | None = None) -> Path:
    """便捷方法:解析 data 目录下的文件。"""
    return resolve_tenant_path(f"data/{filename}", tenant_id)


# =====================================================================
# 租户注册中心(轻量 - 单文件持久化)
# =====================================================================


class TenantRegistry:
    """租户注册中心 - 管理 tenant_id → TenantInfo 映射。

    持久化到 ~/.deadman/tenants/registry.json(单文件,低频写)。
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        self.registry_path = registry_path or (TENANTS_ROOT / "registry.json")
        self._lock = threading.RLock()
        self._cache: dict[str, TenantInfo] = {}
        self._loaded = False

    def register(self, tenant: TenantInfo) -> TenantInfo:
        """注册新租户。"""
        with self._lock:
            self._load()
            self._cache[tenant.tenant_id] = tenant
            self._save()
            # 创建租户目录
            tenant_dir = TENANTS_ROOT / tenant.tenant_id
            (tenant_dir / "memory").mkdir(parents=True, exist_ok=True)
            (tenant_dir / "vault").mkdir(parents=True, exist_ok=True)
            (tenant_dir / "data").mkdir(parents=True, exist_ok=True)
            logger.info("Tenant registered: %s (%s)", tenant.tenant_id, tenant.name)
            return tenant

    def get(self, tenant_id: str) -> TenantInfo | None:
        with self._lock:
            self._load()
            return self._cache.get(tenant_id)

    def list_tenants(self) -> list[TenantInfo]:
        with self._lock:
            self._load()
            return list(self._cache.values())

    def update(self, tenant_id: str, **fields) -> TenantInfo | None:
        """更新租户字段。"""
        with self._lock:
            self._load()
            tenant = self._cache.get(tenant_id)
            if tenant is None:
                return None
            for k, v in fields.items():
                if hasattr(tenant, k):
                    setattr(tenant, k, v)
            self._save()
            return tenant

    def delete(self, tenant_id: str) -> bool:
        """删除租户(仅注册表,数据保留待手动清理)。"""
        with self._lock:
            self._load()
            if tenant_id in self._cache:
                del self._cache[tenant_id]
                self._save()
                return True
            return False

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.registry_path.exists():
                import json

                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                for tid, info in data.get("tenants", {}).items():
                    self._cache[tid] = TenantInfo(
                        tenant_id=tid,
                        name=info.get("name", ""),
                        plan=info.get("plan", "free"),
                        created_at=info.get("created_at", 0.0),
                        quota_token_per_day=info.get("quota_token_per_day", 100_000),
                        quota_tool_calls_per_day=info.get("quota_tool_calls_per_day", 1_000),
                        quota_storage_mb=info.get("quota_storage_mb", 100),
                        features=info.get("features", []),
                    )
        except Exception as e:
            logger.warning("Tenant registry load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            import json

            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "tenants": {tid: t.to_dict() for tid, t in self._cache.items()},
            }
            tmp = self.registry_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.registry_path)
        except Exception as e:
            logger.error("Tenant registry save failed: %s", e)


# 全局单例
_tenant_registry: TenantRegistry | None = None
_registry_lock = threading.Lock()


def get_tenant_registry() -> TenantRegistry:
    global _tenant_registry
    if _tenant_registry is None:
        with _registry_lock:
            if _tenant_registry is None:
                _tenant_registry = TenantRegistry()
    return _tenant_registry
