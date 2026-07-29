"""P8.6.5 数据保留策略 - 7 年法规要求 + 自动过期清理。

法规依据:
    - 中国《会计档案管理办法》:会计凭证 30 年
    - 中国《税收征收管理法》:账簿 10 年
    - GDPR 第 5 条:数据保留期限应限于"必要"
    - 中国《生成式人工智能服务管理暂行办法》:
      训练日志 / 用户反馈保留 ≥ 6 个月(用于追溯)
    - 律所业务记录:律协要求 ≥ 7 年
    - 殡葬服务记录:行业规范 ≥ 7 年(便于纠纷追溯)

设计:
    - DataCategory: 数据分类(每类有独立保留期)
    - RetentionPolicy: 单类保留策略(保留期 + 到期处置)
    - RetentionManager: 全局保留策略管理 + 过期清理

到期处置:
    - DELETE: 彻底删除(默认)
    - ANONYMIZE: 去标识化(保留聚合统计)
    - ARCHIVE: 归档到冷存储
    - KEEP: 保留(强制保留,如审计日志)

feature flag:`DEADMAN_COMPLIANCE_ENABLED=0` 关闭时不清理(透传)
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
from typing import Any
from collections.abc import Callable

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


# 默认保留期(天)
DEFAULT_RETENTION_DAYS = {
    "user_profile": 2555,        # 7 年
    "chat_history": 365,          # 1 年
    "memory_episodes": 365 * 3,   # 3 年
    "memory_semantic": 365 * 7,   # 7 年(用户已确认的知识)
    "memory_procedural": 365 * 7, # 7 年
    "audit_log": 365 * 7,         # 7 年(法规强制)
    "billing_record": 365 * 10,  # 10 年(税务要求)
    "deletion_request": 365 * 7, # 7 年(证明已删)
    "consent_record": 365 * 7,   # 7 年(同意管理凭证)
    "reflexion_memory": 365,     # 1 年
    "vector_embedding": 365,     # 1 年
    "ai_output": 90,             # 90 天(模型输出日志)
    "training_log": 180,         # 6 个月(法规要求)
    "temp_data": 7,              # 7 天(临时数据)
}


class DataCategory(str, Enum):
    """数据分类(决定保留期)。"""

    USER_PROFILE = "user_profile"
    CHAT_HISTORY = "chat_history"
    MEMORY_EPISODIC = "memory_episodes"
    MEMORY_SEMANTIC = "memory_semantic"
    MEMORY_PROCEDURAL = "memory_procedural"
    AUDIT_LOG = "audit_log"
    BILLING_RECORD = "billing_record"
    DELETION_REQUEST = "deletion_request"
    CONSENT_RECORD = "consent_record"
    REFLEXION_MEMORY = "reflexion_memory"
    VECTOR_EMBEDDING = "vector_embedding"
    AI_OUTPUT = "ai_output"
    TRAINING_LOG = "training_log"
    TEMP_DATA = "temp_data"


class DisposalAction(str, Enum):
    """到期处置动作。"""

    DELETE = "delete"          # 彻底删除
    ANONYMIZE = "anonymize"    # 去标识化(保留聚合)
    ARCHIVE = "archive"        # 归档冷存储
    KEEP = "keep"              # 强制保留(如审计日志)


@dataclass
class RetentionPolicy:
    """单类数据保留策略。"""

    category: DataCategory
    retention_days: int  # 保留期(天)
    disposal_action: DisposalAction = DisposalAction.DELETE
    description: str = ""
    # 法规依据(便于审计)
    legal_basis: str = ""
    # 是否可被用户请求缩短(如用户主动删除)
    user_shortenable: bool = False
    # 是否在 deletion_request 触发时优先清理
    deletion_priority: int = 0  # 0=最后 / 10=最先


@dataclass
class RetentionRecord:
    """单条数据保留记录(用于过期扫描)。"""

    category: DataCategory
    user_id: str
    tenant_id: str
    data_id: str  # 数据唯一 ID(文件路径 / 记录 ID)
    created_at: float
    expires_at: float  # created_at + retention_days
    size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        now = now or time.time()
        return now >= self.expires_at

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value
        return d


class RetentionManager:
    """数据保留管理器。

    设计:
        - 全局保留策略(可被租户级覆盖)
        - 注册"清理器"(每类数据一个 callable)
        - 周期性扫描过期数据 → 触发清理
        - 清理日志(证明已合规处置)

    用法:
        rm = get_retention_manager()
        rm.register_cleaner(DataCategory.CHAT_HISTORY, lambda user_id, data_id: ...)
        rm.record(DataCategory.CHAT_HISTORY, user_id, data_id="msg_123")
        # 周期性(每日 cron)调用:
        rm.run_sweep()
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_RETENTION_STORE", "data/compliance/retention.json")
        )
        self._lock = threading.RLock()
        self._policies: dict[DataCategory, RetentionPolicy] = self._init_default_policies()
        # 待扫描记录:{category: [RetentionRecord]}
        self._records: dict[DataCategory, list[RetentionRecord]] = {}
        # 清理日志(已处置记录)
        self._disposal_log: list[dict[str, Any]] = []
        # 清理器:category → callable(user_id, data_id) → bool
        self._cleaners: dict[DataCategory, Callable[[str, str], bool]] = {}
        # 租户级策略覆盖
        self._tenant_overrides: dict[str, dict[DataCategory, RetentionPolicy]] = {}
        self._loaded = False

    def set_policy(self, policy: RetentionPolicy) -> None:
        """设置 / 更新保留策略。"""
        with self._lock:
            self._policies[policy.category] = policy
            self._save()

    def set_tenant_override(
        self,
        tenant_id: str,
        category: DataCategory,
        retention_days: int,
    ) -> None:
        """租户级覆盖(企业版可自定义)。"""
        with self._lock:
            base = self._policies.get(category)
            if base is None:
                return
            override = RetentionPolicy(
                category=category,
                retention_days=retention_days,
                disposal_action=base.disposal_action,
                description=f"{base.description} (tenant override)",
                legal_basis=base.legal_basis,
            )
            self._tenant_overrides.setdefault(tenant_id, {})[category] = override
            self._save()

    def get_policy(
        self,
        category: DataCategory,
        tenant_id: str | None = None,
    ) -> RetentionPolicy:
        """获取策略(优先租户覆盖)。"""
        with self._lock:
            if tenant_id:
                tenant_policies = self._tenant_overrides.get(tenant_id, {})
                if category in tenant_policies:
                    return tenant_policies[category]
            return self._policies.get(
                category,
                RetentionPolicy(category=category, retention_days=DEFAULT_RETENTION_DAYS.get(category.value, 365)),
            )

    def record(
        self,
        category: DataCategory,
        user_id: str,
        data_id: str,
        size_bytes: int = 0,
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RetentionRecord:
        """记录一条数据(用于后续过期扫描)。"""
        if not is_enabled("compliance"):
            return RetentionRecord(
                category=category,
                user_id=user_id,
                tenant_id=tenant_id or "default",
                data_id=data_id,
                created_at=time.time(),
                expires_at=time.time() + 365 * 86400,  # 1 年占位
            )

        tid = tenant_id or get_current_tenant_id() or "default"
        policy = self.get_policy(category, tid)
        now = time.time()
        record = RetentionRecord(
            category=category,
            user_id=user_id,
            tenant_id=tid,
            data_id=data_id,
            created_at=now,
            expires_at=now + policy.retention_days * 86400,
            size_bytes=size_bytes,
            metadata=metadata or {},
        )
        with self._lock:
            self._records.setdefault(category, []).append(record)
            self._save()
        return record

    def register_cleaner(
        self,
        category: DataCategory,
        cleaner: Callable[[str, str], bool],
    ) -> None:
        """注册清理器(category → callable)。"""
        with self._lock:
            self._cleaners[category] = cleaner

    def run_sweep(self, now: float | None = None) -> dict[str, int]:
        """扫描并清理过期数据(由 cron 每日触发)。

        Returns:
            {category: cleaned_count}
        """
        if not is_enabled("compliance"):
            return {}

        now = now or time.time()
        stats: dict[str, int] = {}
        expired_records: list[RetentionRecord] = []

        with self._lock:
            self._load()
            for category, records in self._records.items():
                policy = self.get_policy(category)
                # KEEP 策略不清理
                if policy.disposal_action == DisposalAction.KEEP:
                    continue
                expired = [r for r in records if r.is_expired(now)]
                expired_records.extend(expired)

        # 按 deletion_priority 排序(高优先级先清理)
        expired_records.sort(
            key=lambda r: -self.get_policy(r.category).deletion_priority,
        )

        # 无锁执行清理(允许并发,但 _disposal_log 加锁)
        for record in expired_records:
            policy = self.get_policy(record.category)
            cleaned = self._dispose(record, policy)
            if cleaned:
                stats[record.category.value] = stats.get(record.category.value, 0) + 1
                # 从待扫描列表移除
                with self._lock:
                    if record in self._records.get(record.category, []):
                        self._records[record.category].remove(record)
                    self._disposal_log.append({
                        "category": record.category.value,
                        "user_id": record.user_id,
                        "data_id": record.data_id,
                        "disposal_action": policy.disposal_action.value,
                        "disposed_at": now,
                        "created_at": record.created_at,
                        "retained_days": (now - record.created_at) / 86400,
                    })
                    self._save()

        if stats:
            logger.info("Retention sweep: %s", stats)
        return stats

    def list_expiring(
        self,
        within_days: int = 7,
        tenant_id: str | None = None,
    ) -> list[RetentionRecord]:
        """列出即将过期的数据(预警)。"""
        now = time.time()
        threshold = now + within_days * 86400
        with self._lock:
            self._load()
            expiring: list[RetentionRecord] = []
            for records in self._records.values():
                for r in records:
                    if tenant_id and r.tenant_id != tenant_id:
                        continue
                    if now <= r.expires_at <= threshold:
                        expiring.append(r)
            return expiring

    def get_disposal_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取清理日志(审计用)。"""
        with self._lock:
            self._load()
            return list(self._disposal_log[-limit:])

    # ==================================================================
    # 内部:处置执行
    # ==================================================================

    def _dispose(self, record: RetentionRecord, policy: RetentionPolicy) -> bool:
        """执行处置动作。"""
        cleaner = self._cleaners.get(record.category)
        try:
            if policy.disposal_action == DisposalAction.DELETE:
                if cleaner:
                    return cleaner(record.user_id, record.data_id)
                # 无清理器:仅删除记录(数据本身由其他机制清理)
                return True
            elif policy.disposal_action == DisposalAction.ANONYMIZE:
                if cleaner:
                    return cleaner(record.user_id, record.data_id)
                logger.warning(
                    "No anonymizer registered for %s/%s, disposal skipped",
                    record.category.value, record.data_id,
                )
                return False
            elif policy.disposal_action == DisposalAction.ARCHIVE:
                if cleaner:
                    return cleaner(record.user_id, record.data_id)
                logger.warning(
                    "No archiver registered for %s/%s, disposal skipped",
                    record.category.value, record.data_id,
                )
                return False
            elif policy.disposal_action == DisposalAction.KEEP:
                return False
        except Exception as e:
            logger.error(
                "Dispose %s/%s failed: %s",
                record.category.value, record.data_id, e,
            )
            return False
        return False

    def _init_default_policies(self) -> dict[DataCategory, RetentionPolicy]:
        """初始化默认策略(基于法规要求)。"""
        return {
            DataCategory.USER_PROFILE: RetentionPolicy(
                category=DataCategory.USER_PROFILE,
                retention_days=2555,
                disposal_action=DisposalAction.DELETE,
                description="用户基本信息",
                legal_basis="PIPL 第 19 条(必要范围)",
                user_shortenable=True,
                deletion_priority=10,
            ),
            DataCategory.CHAT_HISTORY: RetentionPolicy(
                category=DataCategory.CHAT_HISTORY,
                retention_days=365,
                disposal_action=DisposalAction.DELETE,
                description="聊天记录",
                legal_basis="PIPL 第 19 条",
                user_shortenable=True,
                deletion_priority=10,
            ),
            DataCategory.MEMORY_EPISODIC: RetentionPolicy(
                category=DataCategory.MEMORY_EPISODIC,
                retention_days=365 * 3,
                disposal_action=DisposalAction.DELETE,
                description="情景记忆",
                legal_basis="产品需求",
                deletion_priority=8,
            ),
            DataCategory.AUDIT_LOG: RetentionPolicy(
                category=DataCategory.AUDIT_LOG,
                retention_days=365 * 7,
                disposal_action=DisposalAction.KEEP,
                description="审计日志(强制保留)",
                legal_basis="等保 2.0 第 8.1.4 条",
                user_shortenable=False,
                deletion_priority=0,
            ),
            DataCategory.BILLING_RECORD: RetentionPolicy(
                category=DataCategory.BILLING_RECORD,
                retention_days=365 * 10,
                disposal_action=DisposalAction.KEEP,
                description="计费记录",
                legal_basis="《税收征收管理法》",
                user_shortenable=False,
                deletion_priority=0,
            ),
            DataCategory.DELETION_REQUEST: RetentionPolicy(
                category=DataCategory.DELETION_REQUEST,
                retention_days=365 * 7,
                disposal_action=DisposalAction.KEEP,
                description="删除请求记录(证明已删)",
                legal_basis="GDPR 第 17 条",
                user_shortenable=False,
                deletion_priority=0,
            ),
            DataCategory.CONSENT_RECORD: RetentionPolicy(
                category=DataCategory.CONSENT_RECORD,
                retention_days=365 * 7,
                disposal_action=DisposalAction.KEEP,
                description="用户同意记录",
                legal_basis="PIPL 第 16 条",
                user_shortenable=False,
                deletion_priority=0,
            ),
            DataCategory.AI_OUTPUT: RetentionPolicy(
                category=DataCategory.AI_OUTPUT,
                retention_days=90,
                disposal_action=DisposalAction.DELETE,
                description="AI 生成内容日志",
                legal_basis="《生成式 AI 管理办法》",
                deletion_priority=5,
            ),
            DataCategory.TRAINING_LOG: RetentionPolicy(
                category=DataCategory.TRAINING_LOG,
                retention_days=180,
                disposal_action=DisposalAction.DELETE,
                description="训练日志",
                legal_basis="《生成式 AI 管理办法》第 7 条(≥6 个月)",
                deletion_priority=5,
            ),
            DataCategory.TEMP_DATA: RetentionPolicy(
                category=DataCategory.TEMP_DATA,
                retention_days=7,
                disposal_action=DisposalAction.DELETE,
                description="临时数据",
                deletion_priority=9,
            ),
        }

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                # 加载 records
                for cat_str, records in data.get("records", {}).items():
                    cat = DataCategory(cat_str)
                    self._records[cat] = [
                        RetentionRecord(
                            category=cat,
                            user_id=r["user_id"],
                            tenant_id=r.get("tenant_id", "default"),
                            data_id=r["data_id"],
                            created_at=r["created_at"],
                            expires_at=r["expires_at"],
                            size_bytes=r.get("size_bytes", 0),
                            metadata=r.get("metadata", {}),
                        )
                        for r in records
                    ]
                # 加载 disposal log
                self._disposal_log = data.get("disposal_log", [])
        except Exception as e:
            logger.warning("Load retention store failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            data = {
                "records": {
                    cat.value: [r.to_dict() for r in records]
                    for cat, records in self._records.items()
                },
                "disposal_log": self._disposal_log[-1000:],  # 只保留最近 1000 条
            }
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Save retention store failed: %s", e)


# 全局单例
_rm_instance: RetentionManager | None = None
_rm_lock = threading.Lock()


def get_retention_manager() -> RetentionManager:
    global _rm_instance
    if _rm_instance is None:
        with _rm_lock:
            if _rm_instance is None:
                _rm_instance = RetentionManager()
    return _rm_instance
