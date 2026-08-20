"""P8.26 AI 决策复议机制 - 用户对 AI 决策不满可申请人工复议。

借鉴行政复议 (Administrative Reconsideration) + GDPR Article 22 (自动化决策反对权),
允许用户对 AI 自动化决策 (如内容审核 / 风险评分 / 拒绝服务) 提出复议,
由人工 reviewer 在 SLA 内审核。

模块结构:
    - Appeal: 单个复议申请 (dataclass)
    - AppealsManager: 复议管理器 (file / review / list + SLA 升级)

设计:
    - SLA:7 天内必须 review,逾期自动 escalate
    - 状态机:FILED → UNDER_REVIEW → APPROVED / REJECTED
    - 持久化到 data/governance/appeals.json (按租户隔离)
    - 原子写 + 线程安全

feature flag:`DEADMAN_GOVERNANCE_ENABLED=0` 关闭时操作静默 no-op。
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

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import resolve_data_path

logger = logging.getLogger(__name__)


# SLA:7 天内必须 review
APPEAL_SLA_SECONDS = 7 * 24 * 3600


class AppealStatus(str, Enum):
    """复议状态机:

    FILED → UNDER_REVIEW → APPROVED
                        ↓
                    REJECTED
    逾期未 review → 自动标记 escalated
    """

    FILED = "filed"  # 已提交
    UNDER_REVIEW = "under_review"  # 审核中
    APPROVED = "approved"  # 通过 (撤销 AI 决策)
    REJECTED = "rejected"  # 驳回 (维持 AI 决策)
    ESCALATED = "escalated"  # SLA 超时升级


class AppealDecision(str, Enum):
    """复议决定 (review 时给出)。"""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class Appeal:
    """单个复议申请。

    Attributes:
        appeal_id: 复议 ID
        user_id: 申请人 ID
        decision_id: 被复议的 AI 决策 ID
        reason: 申请人理由
        status: 状态
        filed_at: 提交时间
        reviewed_at: 审核完成时间 (None=未审核)
        reviewer_id: 审核人 ID
        resolution_text: 审核结论说明
        escalated: 是否已 SLA 升级
        sla_deadline: SLA 截止时间 (filed_at + 7d)
    """

    appeal_id: str
    user_id: str
    decision_id: str
    reason: str = ""
    status: AppealStatus = AppealStatus.FILED
    filed_at: float = field(default_factory=time.time)
    reviewed_at: float | None = None
    reviewer_id: str = ""
    resolution_text: str = ""
    escalated: bool = False
    sla_deadline: float = 0.0

    def __post_init__(self) -> None:
        if self.sla_deadline == 0.0:
            self.sla_deadline = self.filed_at + APPEAL_SLA_SECONDS

    def is_overdue(self, now: float | None = None) -> bool:
        """是否 SLA 超时 (filed 后 7 天未审完)。"""
        if self.status in (AppealStatus.APPROVED, AppealStatus.REJECTED):
            return False
        current = now if now is not None else time.time()
        return current > self.sla_deadline

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Appeal:
        return cls(
            appeal_id=data["appeal_id"],
            user_id=data["user_id"],
            decision_id=data["decision_id"],
            reason=data.get("reason", ""),
            status=AppealStatus(data.get("status", "filed")),
            filed_at=float(data.get("filed_at", time.time())),
            reviewed_at=data.get("reviewed_at"),
            reviewer_id=data.get("reviewer_id", ""),
            resolution_text=data.get("resolution_text", ""),
            escalated=bool(data.get("escalated", False)),
            sla_deadline=float(data.get("sla_deadline", 0.0)),
        )


class AppealsManager:
    """复议管理器 - 受理 / 审核 / SLA 升级。

    用法:
        am = get_appeals_manager()
        appeal = am.file(user_id="u1", decision_id="d1", reason="...")
        am.review(appeal.appeal_id, reviewer_id="rev1",
                  decision=AppealDecision.APPROVED, resolution_text="撤销")
        pending = am.list_pending()
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or resolve_data_path("governance/appeals.json")
        self._lock = threading.RLock()
        self._cache: dict[str, Appeal] = {}
        self._loaded = False
        self._counter = 0

    def file(
        self,
        user_id: str,
        decision_id: str,
        reason: str,
    ) -> Appeal:
        """提交复议申请。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip appeal file")
            return Appeal(
                appeal_id="disabled",
                user_id=user_id,
                decision_id=decision_id,
                reason=reason,
                status=AppealStatus.FILED,
            )
        with self._lock:
            self._load()
            appeal_id = self._generate_id()
            appeal = Appeal(
                appeal_id=appeal_id,
                user_id=user_id,
                decision_id=decision_id,
                reason=reason,
                status=AppealStatus.FILED,
            )
            self._cache[appeal_id] = appeal
            self._save()
            logger.info(
                "Appeal filed: %s (user=%s decision=%s)",
                appeal_id,
                user_id,
                decision_id,
            )
            return appeal

    def review(
        self,
        appeal_id: str,
        reviewer_id: str,
        decision: AppealDecision,
        resolution_text: str,
    ) -> Appeal:
        """审核复议 (人工决定)。"""
        with self._lock:
            self._load()
            appeal = self._cache.get(appeal_id)
            if appeal is None:
                raise KeyError(f"Appeal not found: {appeal_id}")
            if not isinstance(decision, AppealDecision):
                raise ValueError(f"decision must be AppealDecision, got {type(decision)}")
            appeal.reviewer_id = reviewer_id
            appeal.resolution_text = resolution_text
            appeal.reviewed_at = time.time()
            if decision == AppealDecision.APPROVED:
                appeal.status = AppealStatus.APPROVED
            else:
                appeal.status = AppealStatus.REJECTED
            self._save()
            logger.info(
                "Appeal reviewed: %s decision=%s reviewer=%s",
                appeal_id,
                decision.value,
                reviewer_id,
            )
            return appeal

    def get(self, appeal_id: str) -> Appeal | None:
        """按 ID 获取复议。"""
        with self._lock:
            self._load()
            return self._cache.get(appeal_id)

    def list_pending(self) -> list[Appeal]:
        """列出待审核复议 (status=FILED 或 UNDER_REVIEW)。"""
        with self._lock:
            self._load()
            # 先做 SLA 升级检查
            self._check_sla_escalations()
            return [
                a
                for a in self._cache.values()
                if a.status in (AppealStatus.FILED, AppealStatus.UNDER_REVIEW)
            ]

    def list_by_user(self, user_id: str) -> list[Appeal]:
        """按用户列出复议。"""
        with self._lock:
            self._load()
            return [a for a in self._cache.values() if a.user_id == user_id]

    def list_all(self) -> list[Appeal]:
        """列出所有复议。"""
        with self._lock:
            self._load()
            return list(self._cache.values())

    def start_review(self, appeal_id: str, reviewer_id: str) -> Appeal | None:
        """标记开始审核 (状态切到 UNDER_REVIEW)。"""
        with self._lock:
            self._load()
            appeal = self._cache.get(appeal_id)
            if appeal is None:
                return None
            appeal.status = AppealStatus.UNDER_REVIEW
            appeal.reviewer_id = reviewer_id
            self._save()
            return appeal

    def list_overdue(self, now: float | None = None) -> list[Appeal]:
        """列出 SLA 超时的复议。"""
        with self._lock:
            self._load()
            return [a for a in self._cache.values() if a.is_overdue(now)]

    # ==================================================================
    # SLA 升级
    # ==================================================================

    def _check_sla_escalations(self) -> int:
        """检查并标记 SLA 超时的复议。"""
        now = time.time()
        escalated_count = 0
        for appeal in self._cache.values():
            if (
                appeal.status in (AppealStatus.FILED, AppealStatus.UNDER_REVIEW)
                and appeal.is_overdue(now)
                and not appeal.escalated
            ):
                appeal.escalated = True
                appeal.status = AppealStatus.ESCALATED
                escalated_count += 1
                logger.warning(
                    "Appeal %s SLA escalated (filed=%d, deadline=%d)",
                    appeal.appeal_id,
                    int(appeal.filed_at),
                    int(appeal.sla_deadline),
                )
        if escalated_count > 0:
            self._save()
        return escalated_count

    # ==================================================================
    # 内部
    # ==================================================================

    def _generate_id(self) -> str:
        self._counter += 1
        return f"appeal-{int(time.time())}-{self._counter}"

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for aid, adata in data.get("appeals", {}).items():
                    self._cache[aid] = Appeal.from_dict(adata)
                self._counter = int(data.get("counter", 0))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load appeals failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "counter": self._counter,
                "appeals": {aid: a.to_dict() for aid, a in self._cache.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Save appeals failed: %s", e)


# 全局单例
_am_instance: AppealsManager | None = None
_am_lock = threading.Lock()


def get_appeals_manager() -> AppealsManager:
    global _am_instance
    if _am_instance is None:
        with _am_lock:
            if _am_instance is None:
                _am_instance = AppealsManager()
    return _am_instance
