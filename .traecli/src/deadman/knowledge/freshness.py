"""P8.3.3 知识时效管理 - 法规变更检测与归档。

设计目标:
    - 不同类别的知识有不同 TTL(法规 1 年,判例 5 年,用户经验 90 天,AI 生成 30 天)
    - check(knowledge_id) 返回时效报告(fresh / stale)
    - archive_outdated() 批量归档过期知识
    - watch_changes(source) 注册定期检查任务
    - register_source() 注册外部知识源(法律 / 案例文书),含 URL + 解析器

法规依据:
    - 中国《民法典》第 1256 条:法律推定 / 法规变更需重新评估
    - 检索增强生成场景:过期知识 → 错误结论 → 法律责任

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 原子写:持久化用 .tmp + os.replace
    - 线程安全:threading.RLock
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


class KnowledgeCategory(str, Enum):
    """知识类别(不同 TTL)。"""

    LAW = "law"                          # 法律法规(TTL 365 天)
    COURT_CASE = "court_case"            # 法院判例(TTL 1825 天 = 5 年)
    GOVERNMENT_DOC = "government_doc"    # 政府文件(TTL 365 天)
    USER_EXPERIENCE = "user_experience"  # 用户实操经验(TTL 90 天)
    AI_GENERATED = "ai_generated"        # AI 生成内容(TTL 30 天)
    OTHER = "other"                      # 其他(TTL 180 天)


# 各类别的 TTL(天)
CATEGORY_TTL_DAYS: dict[KnowledgeCategory, int] = {
    KnowledgeCategory.LAW: 365,
    KnowledgeCategory.COURT_CASE: 1825,  # 5 年
    KnowledgeCategory.GOVERNMENT_DOC: 365,
    KnowledgeCategory.USER_EXPERIENCE: 90,
    KnowledgeCategory.AI_GENERATED: 30,
    KnowledgeCategory.OTHER: 180,
}


def _classify_by_source(source: str) -> KnowledgeCategory:
    """根据 source 前缀推断类别。"""
    if not source:
        return KnowledgeCategory.OTHER
    s = source.lower()
    if s.startswith("official_law:") or s.startswith("law:"):
        return KnowledgeCategory.LAW
    if s.startswith("court_case:") or s.startswith("case:"):
        return KnowledgeCategory.COURT_CASE
    if s.startswith("government_doc:") or s.startswith("gov:"):
        return KnowledgeCategory.GOVERNMENT_DOC
    if s.startswith("user_experience:") or s.startswith("user:"):
        return KnowledgeCategory.USER_EXPERIENCE
    if s.startswith("ai_generated:") or s.startswith("ai:"):
        return KnowledgeCategory.AI_GENERATED
    return KnowledgeCategory.OTHER


@dataclass
class FreshnessReport:
    """单条知识的时效报告。

    Attributes:
        knowledge_id: 知识 ID(对应 KGNode.id / LightNode.id)
        source: 来源标识
        category: 知识类别
        last_updated: 上次更新时间(epoch 秒)
        age_days: 距今天数(浮点)
        is_stale: 是否过期(True 表示需要刷新 / 归档)
        staleness_reason: 过期原因(如 "TTL 365 天已超")
    """

    knowledge_id: str
    source: str = ""
    category: KnowledgeCategory = KnowledgeCategory.OTHER
    last_updated: float = 0.0
    age_days: float = 0.0
    is_stale: bool = False
    staleness_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FreshnessReport:
        return cls(
            knowledge_id=data["knowledge_id"],
            source=data.get("source", ""),
            category=KnowledgeCategory(data.get("category", "other")),
            last_updated=float(data.get("last_updated", 0.0)),
            age_days=float(data.get("age_days", 0.0)),
            is_stale=bool(data.get("is_stale", False)),
            staleness_reason=data.get("staleness_reason", ""),
        )


@dataclass
class ExternalSource:
    """外部知识源(用于 watch_changes / 定期检查)。

    Attributes:
        name: 源名称(如 "国家法律法规数据库")
        url: 来源 URL
        check_interval: 检查间隔(秒)
        parser: 解析器函数名(用于在 watch_changes 中调用)
        last_checked: 上次检查时间(epoch 秒)
        registered_at: 注册时间(epoch 秒)
    """

    name: str
    url: str
    check_interval: int = 86400  # 默认每天
    parser: str = ""  # 解析器函数名(可调用字符串;实际执行由调用方注入)
    last_checked: float = 0.0
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExternalSource:
        return cls(
            name=data["name"],
            url=data["url"],
            check_interval=int(data.get("check_interval", 86400)),
            parser=data.get("parser", ""),
            last_checked=float(data.get("last_checked", 0.0)),
            registered_at=float(data.get("registered_at", time.time())),
        )


class KnowledgeFreshness:
    """知识时效管理器。

    用法:
        fr = KnowledgeFreshness()
        # 1. 记录某知识的更新时间(通常由 KnowledgeManager.add_knowledge 调用)
        fr.touch("node-abc", source="official_law:cn", ts=time.time())
        # 2. 查询时效
        report = fr.check("node-abc")
        # 3. 批量归档过期
        archived = fr.archive_outdated()
        # 4. 注册外部源
        fr.register_source("国家法律法规库", "https://flk.npc.gov.cn", check_interval=86400)
        # 5. 注册变更监控(供 cron 调用)
        fr.watch_changes("official_law:cn")

    设计:
        - 持久化:_records(knowledge_id → last_updated + source + category)
        - TTL 按类别从 CATEGORY_TTL_DAYS 取
        - archive_outdated 仅标记(返回计数),不实际删除(避免误删)
        - 调用方需提供归档器回调以执行实际归档动作
    """

    def __init__(
        self,
        persist_path: Path | None = None,
        reference_time: float | None = None,
    ) -> None:
        """构造。

        Args:
            persist_path: 持久化路径;None 纯内存
            reference_time: 参考时间(epoch 秒);None 表示当前时间(测试时可注入)
        """
        self.persist_path = persist_path
        self._reference_time = reference_time
        self._lock = threading.RLock()
        # knowledge_id -> {last_updated, source, category}
        self._records: dict[str, dict[str, Any]] = {}
        # name -> ExternalSource
        self._sources: dict[str, ExternalSource] = {}
        # 已 watch 的来源集合
        self._watched_sources: set[str] = set()
        # 归档器回调(knowledge_id -> bool)
        self._archiver: Callable[[str], bool] | None = None
        if persist_path is not None:
            self._load()

    # ==================================================================
    # 记录与查询
    # ==================================================================

    def touch(
        self,
        knowledge_id: str,
        source: str = "",
        ts: float | None = None,
        category: KnowledgeCategory | None = None,
    ) -> None:
        """记录某知识的更新时间。

        Args:
            knowledge_id: 知识 ID
            source: 来源标识(用于推断 category)
            ts: 更新时间(epoch 秒);None 表示当前时间
            category: 显式类别;None 时按 source 推断
        """
        if not is_enabled("knowledge_graph"):
            return
        with self._lock:
            now = ts if ts is not None else time.time()
            cat = category or _classify_by_source(source)
            self._records[knowledge_id] = {
                "last_updated": now,
                "source": source,
                "category": cat.value,
            }
            self._persist()

    def check(self, knowledge_id: str) -> FreshnessReport:
        """查询某知识的时效报告。

        Args:
            knowledge_id: 知识 ID

        Returns:
            FreshnessReport;若未记录则返回 stale=True 的默认报告
        """
        if not is_enabled("knowledge_graph"):
            # flag 关闭时返回空报告(不视为 stale)
            return FreshnessReport(
                knowledge_id=knowledge_id,
                is_stale=False,
                staleness_reason="knowledge_graph disabled",
            )

        with self._lock:
            rec = self._records.get(knowledge_id)
            now = self._reference_time if self._reference_time is not None else time.time()
            if rec is None:
                # 未记录视为过期
                return FreshnessReport(
                    knowledge_id=knowledge_id,
                    is_stale=True,
                    staleness_reason="未记录更新时间",
                )
            last_updated = float(rec.get("last_updated", 0.0))
            cat_str = rec.get("category", "other")
            try:
                cat = KnowledgeCategory(cat_str)
            except ValueError:
                cat = KnowledgeCategory.OTHER
            ttl = CATEGORY_TTL_DAYS.get(cat, 180)
            age_seconds = max(0.0, now - last_updated)
            age_days = age_seconds / 86400.0
            is_stale = age_days > ttl
            reason = ""
            if is_stale:
                reason = f"{cat.value} TTL {ttl} 天已超(实际 {age_days:.1f} 天)"
            return FreshnessReport(
                knowledge_id=knowledge_id,
                source=rec.get("source", ""),
                category=cat,
                last_updated=last_updated,
                age_days=age_days,
                is_stale=is_stale,
                staleness_reason=reason,
            )

    # ==================================================================
    # 归档
    # ==================================================================

    def set_archiver(self, archiver: Callable[[str], bool]) -> None:
        """注入归档器回调。

        Args:
            archiver: callable(knowledge_id: str) → bool(True 表示归档成功)
        """
        self._archiver = archiver

    def archive_outdated(self) -> int:
        """批量归档过期知识。

        流程:
            1. 遍历所有记录,找出 is_stale=True 的
            2. 若注入了 archiver 回调 → 调用之(实际归档动作)
            3. 从 freshness 记录中移除已归档的(下次访问视为未记录 → stale)
            4. 返回归档的数量

        Returns:
            归档条数(flag 关闭时返回 0)
        """
        if not is_enabled("knowledge_graph"):
            return 0

        with self._lock:
            outdated: list[str] = []
            for kid in list(self._records.keys()):
                report = self.check(kid)
                if report.is_stale:
                    outdated.append(kid)

            archived_count = 0
            for kid in outdated:
                if self._archiver is not None:
                    try:
                        ok = self._archiver(kid)
                        if not ok:
                            continue
                    except Exception as e:
                        logger.warning("Archiver failed for %s: %s", kid, e)
                        continue
                # 从记录中移除(下次访问视为未记录 → stale)
                del self._records[kid]
                archived_count += 1
            if archived_count:
                self._persist()
            logger.info("Archived %d outdated knowledge items", archived_count)
            return archived_count

    def check_all(self) -> list[FreshnessReport]:
        """检查所有记录的时效,返回完整报告列表。"""
        with self._lock:
            return [self.check(kid) for kid in self._records]

    # ==================================================================
    # 外部源注册与变更监控
    # ==================================================================

    def register_source(
        self,
        name: str,
        url: str,
        check_interval: int = 86400,
        parser: str = "",
    ) -> ExternalSource:
        """注册外部知识源(法律 / 案例文书 / 政策数据库)。

        Args:
            name: 源名称
            url: 来源 URL
            check_interval: 检查间隔(秒),默认 86400(每天)
            parser: 解析器函数名(供 watch_changes 调用)

        Returns:
            ExternalSource 对象
        """
        with self._lock:
            src = ExternalSource(
                name=name,
                url=url,
                check_interval=check_interval,
                parser=parser,
            )
            self._sources[name] = src
            self._persist()
            logger.info("Registered external source: %s (%s)", name, url)
            return src

    def list_sources(self) -> list[ExternalSource]:
        """列出所有已注册的外部源。"""
        with self._lock:
            return list(self._sources.values())

    def watch_changes(self, source: str) -> None:
        """为指定 source 调度定期检查任务。

        本期实现:仅记录 watch 意图(供 cron 调用方拉起),
        不真正启动后台线程(避免测试环境副作用)。

        Args:
            source: 来源标识(如 "official_law:cn")
        """
        if not is_enabled("knowledge_graph"):
            return
        with self._lock:
            # 标记为已 watch(以 source 为 key)
            self._watched_sources.add(source)
            logger.info("Watching source for changes: %s", source)

    def get_watched_sources(self) -> list[str]:
        """获取已 watch 的来源列表(供 cron 拉起)。"""
        with self._lock:
            return list(self._watched_sources)

    def mark_source_checked(self, name: str, ts: float | None = None) -> None:
        """标记外部源已检查(更新 last_checked)。"""
        with self._lock:
            src = self._sources.get(name)
            if src is None:
                return
            src.last_checked = ts if ts is not None else time.time()
            self._persist()

    # ==================================================================
    # 持久化
    # ==================================================================

    def _persist(self) -> None:
        if self.persist_path is None:
            return
        try:
            data = {
                "version": 1,
                "updated_at": time.time(),
                "records": self._records,
                "sources": {n: s.to_dict() for n, s in self._sources.items()},
                "watched": list(self._watched_sources),
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.persist_path)
        except OSError as e:
            logger.error("KnowledgeFreshness persist failed: %s", e)

    def _load(self) -> None:
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            text = self.persist_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            self._records = dict(data.get("records", {}))
            for n, s in data.get("sources", {}).items():
                self._sources[n] = ExternalSource.from_dict(s)
            self._watched_sources = set(data.get("watched", []))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("KnowledgeFreshness load failed, using empty: %s", e)


__all__ = [
    "CATEGORY_TTL_DAYS",
    "ExternalSource",
    "FreshnessReport",
    "KnowledgeCategory",
    "KnowledgeFreshness",
    "_classify_by_source",
]
