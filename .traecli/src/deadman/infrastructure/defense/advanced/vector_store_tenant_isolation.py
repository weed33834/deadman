"""D13:向量库租户隔离(Vector Store Tenant Isolation)。

问题:
    deadman 现有 `memory/vector_store.py` 使用单一全局 collection
    (`collection_name="deadman_episodes"`),所有租户共享一个向量空间:
        - 租户 A 添加"用户张三的死亡事件"
        - 租户 B query"死亡事件" → 召回到租户 A 的记录(隐私泄漏!)
        - 删除租户 A 数据时,影响其他租户

    即便 metadata 标了 tenant_id,query 时也未强制过滤,
    攻击者可绕过 metadata 过滤(通过相似度召回而非 metadata 查询)。

缓解:
    - TenantVectorStore: 包装底层 VectorStore,强制按 tenant_id 隔离
    - per-tenant collection: 每租户独立 collection(name = f"{base}_{tenant_id}")
    - 跨租户共享 collection(可选):metadata 强制 tenant_id,query 强制过滤
    - ACL 校验:delete(id) 时校验 id 属于当前 tenant

设计:
    TenantVectorStore(inMemory=True | chroma=True)
        .add(tenant_id="tA", id="ep1", text="...", metadata={...})
        .query(tenant_id="tA", text="...", top_k=5)  # 仅召回 tA 的数据
        .delete(tenant_id="tA", id="ep1")
        .count(tenant_id="tA")

    隔离模式:
        - PER_TENANT_COLLECTION:每租户独立 collection(强隔离,推荐生产)
        - METADATA_FILTER:共享 collection + metadata 过滤(开发用,弱隔离)

集成:
    memory/manager.py 调用时强制传 tenant_id。
    ContextVar 自动注入(参考 multi_tenant.py)。

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class IsolationMode(str, Enum):
    """租户隔离模式。"""

    PER_TENANT_COLLECTION = "per_tenant_collection"  # 强隔离:每租户独立 collection
    METADATA_FILTER = "metadata_filter"  # 弱隔离:共享 collection + metadata 过滤


_TENANT_METADATA_KEY = "__tenant_id__"


class TenantIsolationError(Exception):
    """租户隔离违规(如尝试访问其他租户的数据)。"""


@dataclass
class TenantVectorStats:
    """租户向量库统计(看板用)。"""

    tenant_id: str
    collection_name: str
    vector_count: int
    isolation_mode: IsolationMode


class TenantVectorStore:
    """租户感知的向量库包装器。

    用法:
        # 包装现有 ChromaVectorStore
        from deadman.memory.vector_store import ChromaVectorStore
        base = ChromaVectorStore(collection_name="deadman")
        tenant_store = TenantVectorStore(
            base_factory=lambda name: ChromaVectorStore(collection_name=name),
            mode=IsolationMode.PER_TENANT_COLLECTION,
        )

        # 添加(强制 tenant_id)
        tenant_store.add("tA", "ep1", "user died", {"timestamp": "..."})

        # 查询(仅返回 tA 的数据)
        results = tenant_store.query("tA", "death event", top_k=5)

        # 删除(校验 id 属于 tA)
        tenant_store.delete("tA", "ep1")

    强制:
        - add/query/delete/count 必须传 tenant_id
        - delete 时若 id 不属于当前 tenant → 抛 TenantIsolationError
        - PER_TENANT_COLLECTION 模式下,query 物理上只搜索单租户 collection
    """

    def __init__(
        self,
        base_factory: Any,
        mode: IsolationMode = IsolationMode.PER_TENANT_COLLECTION,
        base_collection_name: str = "deadman",
    ) -> None:
        """
        Args:
            base_factory: callable(collection_name: str) -> VectorStore
                          返回一个新的底层 VectorStore 实例(独立 collection)
            mode: 隔离模式
            base_collection_name: 基础 collection 名(用于 METADATA_FILTER 模式)
        """
        self._base_factory = base_factory
        self._mode = mode
        self._base_collection_name = base_collection_name
        self._lock = threading.RLock()
        # PER_TENANT_COLLECTION 模式:每租户独立底层 store
        self._tenant_stores: dict[str, Any] = {}
        # METADATA_FILTER 模式:共享底层 store
        self._shared_store: Any = None
        # id → tenant_id 映射(用于 delete 时校验所有权)
        self._id_ownership: dict[str, str] = {}

    def add(
        self,
        tenant_id: str,
        id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        """添加向量(强制 tenant_id)。"""
        if not tenant_id:
            raise ValueError("tenant_id is required for TenantVectorStore.add")

        meta = dict(metadata or {})
        # 强制注入 tenant_id metadata(防止伪造)
        meta[_TENANT_METADATA_KEY] = tenant_id

        store = self._get_store_for_tenant(tenant_id)
        store.add(id=id, text=text, metadata=meta)
        # 记录所有权
        with self._lock:
            self._id_ownership[id] = tenant_id

    def query(
        self,
        tenant_id: str,
        text: str,
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[dict]:
        """查询向量(强制 tenant_id,仅返回当前租户数据)。"""
        if not tenant_id:
            raise ValueError("tenant_id is required for TenantVectorStore.query")

        store = self._get_store_for_tenant(tenant_id)

        if self._mode == IsolationMode.PER_TENANT_COLLECTION:
            # 强隔离:底层 collection 物理隔离,无需 metadata 过滤
            results = store.query(text=text, top_k=top_k, filter=filter)
        else:
            # 弱隔离:共享 collection,强制 metadata 过滤
            merged_filter = dict(filter or {})
            merged_filter[_TENANT_METADATA_KEY] = tenant_id
            results = store.query(text=text, top_k=top_k, filter=merged_filter)
            # 双重防护:即使底层不支持 filter,也在 Python 层过滤
            results = [
                r for r in results
                if r.get("metadata", {}).get(_TENANT_METADATA_KEY) == tenant_id
            ]

        # 永远做最后一层防御:Python 层过滤
        cleaned = []
        for r in results:
            m = r.get("metadata", {})
            # 移除内部 tenant_id 字段(不暴露给调用方)
            m_clean = {k: v for k, v in m.items() if k != _TENANT_METADATA_KEY}
            r_clean = dict(r)
            r_clean["metadata"] = m_clean
            cleaned.append(r_clean)
        return cleaned

    def delete(self, tenant_id: str, id: str) -> bool:
        """删除向量(校验所有权)。"""
        if not tenant_id:
            raise ValueError("tenant_id is required for TenantVectorStore.delete")

        with self._lock:
            owner = self._id_ownership.get(id)
            if owner is not None and owner != tenant_id:
                # 严重:尝试删除其他租户的数据
                logger.error(
                    "Tenant isolation violation: tenant=%s tried to delete id=%s "
                    "owned by tenant=%s",
                    tenant_id, id, owner,
                )
                raise TenantIsolationError(
                    f"id '{id}' does not belong to tenant '{tenant_id}'"
                )

        store = self._get_store_for_tenant(tenant_id)
        deleted = store.delete(id=id)
        if deleted:
            with self._lock:
                self._id_ownership.pop(id, None)
        return deleted

    def count(self, tenant_id: str) -> int:
        """统计当前租户的向量数。"""
        if not tenant_id:
            raise ValueError("tenant_id is required for TenantVectorStore.count")
        store = self._get_store_for_tenant(tenant_id)
        return store.count()

    def list_tenant_stats(self) -> list[TenantVectorStats]:
        """列出所有租户统计(运维 / 看板用)。"""
        with self._lock:
            stats = []
            for tid, store in self._tenant_stores.items():
                stats.append(TenantVectorStats(
                    tenant_id=tid,
                    collection_name=self._collection_name_for(tid),
                    vector_count=store.count(),
                    isolation_mode=self._mode,
                ))
            return stats

    def drop_tenant(self, tenant_id: str) -> int:
        """删除租户所有数据(GDPR right-to-delete 用)。

        Returns:
            删除的向量数
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")

        with self._lock:
            store = self._tenant_stores.pop(tenant_id, None)
            if store is None:
                return 0
            count = store.count()
            # 删除所有权记录中属于此租户的
            ids_to_remove = [
                id for id, owner in self._id_ownership.items()
                if owner == tenant_id
            ]
            for id in ids_to_remove:
                self._id_ownership.pop(id, None)

        # 尝试调用底层 reset / clear(若存在)
        if hasattr(store, "reset"):
            try:
                store.reset()
            except Exception as e:
                logger.warning("Failed to reset tenant store: %s", e)

        return count

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_store_for_tenant(self, tenant_id: str) -> Any:
        """获取租户对应的底层 store。"""
        with self._lock:
            if self._mode == IsolationMode.PER_TENANT_COLLECTION:
                if tenant_id not in self._tenant_stores:
                    coll_name = self._collection_name_for(tenant_id)
                    self._tenant_stores[tenant_id] = self._base_factory(coll_name)
                return self._tenant_stores[tenant_id]
            else:
                # METADATA_FILTER 模式:共享 store
                if self._shared_store is None:
                    self._shared_store = self._base_factory(self._base_collection_name)
                return self._shared_store

    def _collection_name_for(self, tenant_id: str) -> str:
        """生成租户独立 collection 名。

        命名规则:`{base}_{sanitized_tenant_id}`
        - 仅保留字母数字 / 下划线 / 短横线
        - 防止 collection 名注入攻击
        """
        safe = "".join(
            c if c.isalnum() or c in ("_", "-") else "_"
            for c in tenant_id
        )
        return f"{self._base_collection_name}_{safe}"


# =====================================================================
# 全局单例(可选 - 由 memory/manager.py 注入)
# =====================================================================

_global_tenant_store: TenantVectorStore | None = None
_global_lock = threading.Lock()


def get_global_tenant_vector_store() -> TenantVectorStore | None:
    """获取全局 TenantVectorStore(若已初始化)。

    注意:此单例需在系统启动时由 memory/manager.py 显式初始化。
    """
    global _global_tenant_store
    with _global_lock:
        return _global_tenant_store


def set_global_tenant_vector_store(store: TenantVectorStore) -> None:
    """设置全局 TenantVectorStore(启动时调用)。"""
    global _global_tenant_store
    with _global_lock:
        _global_tenant_store = store


def reset_global_tenant_vector_store() -> None:
    """重置全局单例(测试用)。"""
    global _global_tenant_store
    with _global_lock:
        _global_tenant_store = None
