"""P8.4.1 Agent Listing 注册中心 - 提交 / 审核 / 浏览 / 搜索 / 升级。

设计:
    - AgentListing: 单条 marketplace listing 数据(A2A agent_card + marketplace 元信息)
    - MarketplaceRegistry: 注册中心,持有所有 listing,提供 admin 审核 + 用户浏览
    - 持久化: `data/marketplace/registry.json`(原子写),tenant-aware via resolve_data_path
    - 线程安全: 单实例 RLock + 跨实例靠 os.replace 原子性

状态机:
    pending → approved ↔ suspended
           ↘ rejected

feature flag: `DEADMAN_MARKETPLACE_ENABLED=0`(默认关闭)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path

logger = logging.getLogger(__name__)


# =====================================================================
# 异常
# =====================================================================
class MarketplaceError(Exception):
    """marketplace 模块统一异常。

    - feature flag 关闭时所有 API 抛此异常
    - listing 不存在 / 状态非法 / 参数错误也抛此异常
    """


# =====================================================================
# 枚举
# =====================================================================
class ListingStatus(str, Enum):
    """listing 生命周期状态。"""

    PENDING = "pending"        # 待审核(初始)
    APPROVED = "approved"      # 已通过(可上架)
    REJECTED = "rejected"      # 已拒绝(审核失败)
    SUSPENDED = "suspended"    # 已暂停(违规 / 投诉)


class ListingCategory(str, Enum):
    """listing 业务分类(用于浏览过滤)。"""

    LEGAL = "legal"            # 法律
    FINANCE = "finance"        # 财务
    HEALTH = "health"          # 健康
    EDUCATION = "education"    # 教育
    PRODUCTIVITY = "productivity"  # 生产力
    LIFESTYLE = "lifestyle"    # 生活方式
    OTHER = "other"            # 其他


class ListingSort(str, Enum):
    """浏览排序方式。"""

    NEWEST = "newest"          # 按 created_at 倒序
    NAME = "name"              # 按名称字母序
    PRICE_ASC = "price_asc"    # 价格升序
    PRICE_DESC = "price_desc"  # 价格降序


# =====================================================================
# 数据模型
# =====================================================================
@dataclass
class AgentListing:
    """单条 marketplace listing。

    Attributes:
        agent_id: 全局唯一 agent ID(也作为 listing_id 使用)
        name: 展示名(中文 / 英文)
        author: 作者(author_id)
        version: semver 版本号(如 "1.0.0")
        description: 长描述(供 search / 展示)
        category: ListingCategory.value
        tags: 标签列表(供 search)
        price_per_call: 单次调用价格(纯记账,无真实资金流)
        created_at: epoch
        status: ListingStatus.value
        agent_card: A2A AgentCard JSON(dict)
        updated_at: 最后更新时间
        review_reason: 审核备注(reject / suspend reason)
    """

    agent_id: str
    name: str
    author: str
    version: str
    description: str
    category: str
    tags: list[str] = field(default_factory=list)
    price_per_call: float = 0.0
    created_at: float = field(default_factory=time.time)
    status: str = ListingStatus.PENDING.value
    agent_card: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    review_reason: str = ""

    def __post_init__(self) -> None:
        # 规范化枚举字段(允许字符串构造)
        if isinstance(self.status, ListingStatus):
            self.status = self.status.value
        if isinstance(self.category, ListingCategory):
            self.category = self.category.value
        self.updated_at = self.updated_at or self.created_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status
        d["category"] = self.category
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentListing":
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            author=data["author"],
            version=data["version"],
            description=data["description"],
            category=data["category"],
            tags=list(data.get("tags", []) or []),
            price_per_call=float(data.get("price_per_call", 0.0)),
            created_at=float(data.get("created_at", time.time())),
            status=data.get("status", ListingStatus.PENDING.value),
            agent_card=dict(data.get("agent_card", {}) or {}),
            updated_at=float(data.get("updated_at", time.time())),
            review_reason=data.get("review_reason", ""),
        )

    @property
    def listing_id(self) -> str:
        """listing_id 与 agent_id 同源(每个 agent 在每个租户下唯一 listing)。"""
        return self.agent_id


# =====================================================================
# Registry
# =====================================================================
class MarketplaceRegistry:
    """marketplace 注册中心。

    线程安全: 单实例 `threading.RLock` 保护内存结构,持久化靠 `.tmp + os.replace` 原子写。
    多租户: 数据路径通过 `resolve_data_path("marketplace/registry.json")` 解析,
            不同租户的数据自动隔离到 `~/.deadman/tenants/<tid>/data/marketplace/`。
    """

    DEFAULT_STORE_REL = "marketplace/registry.json"

    def __init__(self, store_path: Optional[Path] = None) -> None:
        # store_path 显式传入时直接用(便于测试);否则按当前租户动态解析
        self._explicit_store = store_path
        self._lock = threading.RLock()
        self._cache: dict[str, AgentListing] = {}  # agent_id → listing
        # 当前 cache 对应的 store path(用于检测 tenant 切换,触发重新 load)
        self._loaded_path: Optional[str] = None

    # ==================================================================
    # 路径解析
    # ==================================================================
    def _resolve_store_path(self) -> Path:
        if self._explicit_store is not None:
            return self._explicit_store
        return resolve_data_path(self.DEFAULT_STORE_REL)

    # ==================================================================
    # 提交 / 审核
    # ==================================================================
    def submit(self, listing: AgentListing) -> str:
        """提交新 listing(初始 status=pending)。

        Returns:
            listing_id(= agent_id)

        Raises:
            MarketplaceError: flag 关闭 / agent_id 已存在
        """
        self._require_enabled()
        with self._lock:
            self._load()
            if listing.agent_id in self._cache:
                raise MarketplaceError(
                    f"Agent {listing.agent_id} already submitted"
                )
            # 强制初始状态为 pending(忽略调用方传入的 status)
            listing.status = ListingStatus.PENDING.value
            listing.created_at = time.time()
            listing.updated_at = listing.created_at
            listing.review_reason = ""
            self._cache[listing.agent_id] = listing
            self._save()
            logger.info(
                "Marketplace listing submitted: agent=%s author=%s tenant=%s",
                listing.agent_id, listing.author, get_current_tenant_id(),
            )
            return listing.listing_id

    def approve(self, listing_id: str) -> bool:
        """admin 审核通过(pending → approved)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            listing = self._cache.get(listing_id)
            if listing is None:
                raise MarketplaceError(f"Listing {listing_id} not found")
            if listing.status != ListingStatus.PENDING.value:
                raise MarketplaceError(
                    f"Listing {listing_id} not pending (current={listing.status})"
                )
            listing.status = ListingStatus.APPROVED.value
            listing.updated_at = time.time()
            listing.review_reason = ""
            self._save()
            return True

    def reject(self, listing_id: str, reason: str = "") -> bool:
        """admin 拒绝(pending → rejected)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            listing = self._cache.get(listing_id)
            if listing is None:
                raise MarketplaceError(f"Listing {listing_id} not found")
            if listing.status != ListingStatus.PENDING.value:
                raise MarketplaceError(
                    f"Listing {listing_id} not pending (current={listing.status})"
                )
            listing.status = ListingStatus.REJECTED.value
            listing.updated_at = time.time()
            listing.review_reason = reason
            self._save()
            return True

    def suspend(self, listing_id: str, reason: str = "") -> bool:
        """admin 暂停(approved → suspended)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            listing = self._cache.get(listing_id)
            if listing is None:
                raise MarketplaceError(f"Listing {listing_id} not found")
            if listing.status != ListingStatus.APPROVED.value:
                raise MarketplaceError(
                    f"Listing {listing_id} not approved (current={listing.status})"
                )
            listing.status = ListingStatus.SUSPENDED.value
            listing.updated_at = time.time()
            listing.review_reason = reason
            self._save()
            return True

    def reinstate(self, listing_id: str) -> bool:
        """从 suspended 恢复到 approved(辅助状态机完整性)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            listing = self._cache.get(listing_id)
            if listing is None:
                raise MarketplaceError(f"Listing {listing_id} not found")
            if listing.status != ListingStatus.SUSPENDED.value:
                raise MarketplaceError(
                    f"Listing {listing_id} not suspended (current={listing.status})"
                )
            listing.status = ListingStatus.APPROVED.value
            listing.updated_at = time.time()
            listing.review_reason = ""
            self._save()
            return True

    # ==================================================================
    # 浏览 / 查询
    # ==================================================================
    def list(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        sort_by: str = ListingSort.NEWEST.value,
    ) -> list[AgentListing]:
        """浏览已 approved 的 listing。

        Args:
            query: 子串过滤(name / description,可选)
            category: ListingCategory.value 过滤(可选)
            sort_by: ListingSort.value(默认 newest)
        """
        self._require_enabled()
        with self._lock:
            self._load()
            results: list[AgentListing] = [
                l for l in self._cache.values()
                if l.status == ListingStatus.APPROVED.value
            ]
            if category is not None:
                results = [l for l in results if l.category == category]
            if query:
                q = query.lower()
                results = [
                    l for l in results
                    if q in l.name.lower() or q in l.description.lower()
                ]
            # 排序
            try:
                sort_enum = ListingSort(sort_by)
            except ValueError:
                sort_enum = ListingSort.NEWEST
            if sort_enum == ListingSort.NEWEST:
                results.sort(key=lambda l: l.created_at, reverse=True)
            elif sort_enum == ListingSort.NAME:
                results.sort(key=lambda l: l.name)
            elif sort_enum == ListingSort.PRICE_ASC:
                results.sort(key=lambda l: l.price_per_call)
            elif sort_enum == ListingSort.PRICE_DESC:
                results.sort(key=lambda l: l.price_per_call, reverse=True)
            return results

    def get(self, agent_id: str) -> Optional[AgentListing]:
        """按 agent_id 查询(任意状态)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            return self._cache.get(agent_id)

    def get_listing(self, listing_id: str) -> Optional[AgentListing]:
        """按 listing_id 查询(listing_id == agent_id)。"""
        return self.get(listing_id)

    def search(self, keyword: str) -> list[AgentListing]:
        """全文搜索(仅 approved),匹配 name / description / tags。

        匹配规则: 关键词小写后任一字段包含即返回。
        """
        self._require_enabled()
        with self._lock:
            self._load()
            kw = (keyword or "").lower()
            if not kw:
                return []
            results: list[AgentListing] = []
            for l in self._cache.values():
                if l.status != ListingStatus.APPROVED.value:
                    continue
                haystacks = [l.name.lower(), l.description.lower()]
                haystacks.extend(t.lower() for t in l.tags)
                if any(kw in h for h in haystacks):
                    results.append(l)
            # 默认按 newest 排序
            results.sort(key=lambda l: l.created_at, reverse=True)
            return results

    def update_version(
        self,
        agent_id: str,
        new_version: str,
        new_card: Optional[dict[str, Any]] = None,
    ) -> bool:
        """升级 agent 版本(已 approved 的 listing 才能升级,升级后保持 approved)。

        Args:
            agent_id: 目标 agent
            new_version: 新 semver 版本号
            new_card: 新 agent_card(可选,None 表示仅改 version)
        """
        self._require_enabled()
        with self._lock:
            self._load()
            listing = self._cache.get(agent_id)
            if listing is None:
                raise MarketplaceError(f"Agent {agent_id} not found")
            if listing.status != ListingStatus.APPROVED.value:
                raise MarketplaceError(
                    f"Agent {agent_id} not approved (current={listing.status})"
                )
            listing.version = new_version
            if new_card is not None:
                listing.agent_card = dict(new_card)
            listing.updated_at = time.time()
            self._save()
            return True

    # ==================================================================
    # 内部: 持久化
    # ==================================================================
    def _load(self) -> None:
        """从磁盘加载(惰性,store path 变化时强制重新 load,保证 tenant 隔离正确)。"""
        store = self._resolve_store_path()
        store_key = str(store)
        if store_key == self._loaded_path:
            return  # 当前 cache 已是该 path 的数据
        try:
            new_cache: dict[str, AgentListing] = {}
            if store.exists():
                text = store.read_text(encoding="utf-8")
                data = json.loads(text) if text.strip() else {}
                # 兼容旧格式: 顶层直接是 listings 列表 / dict
                listings_data = data.get("listings", {}) if isinstance(data, dict) else {}
                # listings_data 可能是 dict{agent_id: {...}} 或 list[{...}]
                if isinstance(listings_data, dict):
                    items = listings_data.values()
                else:
                    items = listings_data
                new_cache = {
                    l_data["agent_id"]: AgentListing.from_dict(l_data)
                    for l_data in items
                    if isinstance(l_data, dict) and "agent_id" in l_data
                }
            self._cache = new_cache
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Marketplace registry load failed (%s): %s", store, e)
            self._cache = {}
        self._loaded_path = store_key

    def _save(self) -> None:
        """原子写入(.tmp + os.replace)。"""
        store = self._resolve_store_path()
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "tenant_id": get_current_tenant_id(),
                "listings": {
                    aid: l.to_dict() for aid, l in self._cache.items()
                },
            }
            tmp_path = store.with_suffix(store.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, store)
        except OSError as e:
            logger.error("Marketplace registry save failed (%s): %s", store, e)
            raise MarketplaceError(f"Registry save failed: {e}") from e

    def _require_enabled(self) -> None:
        if not is_enabled("marketplace"):
            raise MarketplaceError(
                "Marketplace feature is disabled (set DEADMAN_MARKETPLACE_ENABLED=1)"
            )


# =====================================================================
# 全局单例
# =====================================================================
_registry_instance: Optional[MarketplaceRegistry] = None
_registry_lock = threading.Lock()


def get_marketplace_registry() -> MarketplaceRegistry:
    """获取全局 MarketplaceRegistry 单例(无显式 store_path,按当前租户解析)。"""
    global _registry_instance
    if _registry_instance is None:
        with _registry_lock:
            if _registry_instance is None:
                _registry_instance = MarketplaceRegistry()
    return _registry_instance
