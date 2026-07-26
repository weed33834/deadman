"""P8.17 AI 治理框架 - 风险卡 (Risk Card + Risk Assessment)。

借鉴 NIST AI Risk Management Framework (AI RMF 1.0, 2023) 和
ISO 31000 风险管理标准,为每个识别出的 AI 风险建立卡片,跟踪
severity / likelihood / mitigation / status。

模块结构:
    - RiskCard: 单个风险的元数据卡 (dataclass)
    - RiskScore: 风险评分结果 (severity × likelihood → score)
    - RiskAssessment: 风险评估器 (注册卡 + 矩阵评分 + 列表 + 缓解 / 接受)

设计:
    - 风险矩阵:severity (1-5) × likelihood (1-5) → score (1-25)
    - 高分 (>= 15) 强制要求 review,>= 20 必须上报 ethics committee
    - 风险状态机:open → mitigating → closed / accepted
    - accepted 状态需正式 reason (签字级别)
    - 持久化到 data/governance/risk_cards.json (按租户隔离)
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
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import resolve_data_path

logger = logging.getLogger(__name__)


class RiskCategory(str, Enum):
    """风险类别 (借鉴 NIST AI RMF)。"""

    SAFETY = "safety"  # 物理安全 (误诊 / 误导操作)
    PRIVACY = "privacy"  # 隐私 (PII 泄漏)
    LEGAL = "legal"  # 法律合规 (违规 / 责任)
    OPERATIONAL = "operational"  # 运营 (服务中断 / 性能)
    ETHICAL = "ethical"  # 伦理 (偏见 / 歧视 / 操纵)


class RiskSeverity(str, Enum):
    """风险严重度 (1-5)。"""

    LOW = "low"  # 1 - 可忽略
    MEDIUM = "medium"  # 2 - 轻微影响
    HIGH = "high"  # 3 - 显著影响
    CRITICAL = "critical"  # 4 - 严重后果
    CATASTROPHIC = "catastrophic"  # 5 - 灾难性

    def rank(self) -> int:
        return {
            RiskSeverity.LOW: 1,
            RiskSeverity.MEDIUM: 2,
            RiskSeverity.HIGH: 3,
            RiskSeverity.CRITICAL: 4,
            RiskSeverity.CATASTROPHIC: 5,
        }[self]


class RiskLikelihood(str, Enum):
    """风险可能性 (1-5)。"""

    RARE = "rare"  # 1 - 极不可能
    UNLIKELY = "unlikely"  # 2 - 不太可能
    POSSIBLE = "possible"  # 3 - 有可能
    LIKELY = "likely"  # 4 - 很可能
    CERTAIN = "certain"  # 5 - 几乎确定

    def rank(self) -> int:
        return {
            RiskLikelihood.RARE: 1,
            RiskLikelihood.UNLIKELY: 2,
            RiskLikelihood.POSSIBLE: 3,
            RiskLikelihood.LIKELY: 4,
            RiskLikelihood.CERTAIN: 5,
        }[self]


class RiskStatus(str, Enum):
    """风险状态机:

    OPEN → MITIGATING → CLOSED
                    ↓
                ACCEPTED (正式接受,需 reason)
    """

    OPEN = "open"  # 已识别,待处理
    MITIGATING = "mitigating"  # 缓解中
    CLOSED = "closed"  # 已关闭 (缓解完成)
    ACCEPTED = "accepted"  # 正式接受 (签字级别,需 reason)


@dataclass
class RiskScore:
    """风险评估分数。

    score = severity × likelihood (1-25)。
    阈值:
        - score >= 20: 必须上报 ethics committee
        - score >= 15: 强制 review
        - score >= 10: 关注
        - score < 10: 可接受
    """

    score: int
    severity: RiskSeverity
    likelihood: RiskLikelihood
    requires_review: bool
    requires_ethics_committee: bool
    level: str  # "low" / "medium" / "high" / "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "severity": self.severity.value,
            "likelihood": self.likelihood.value,
            "requires_review": self.requires_review,
            "requires_ethics_committee": self.requires_ethics_committee,
            "level": self.level,
        }


# 阈值常量
REVIEW_THRESHOLD = 15  # score >= 15 强制 review
ETHICS_COMMITTEE_THRESHOLD = 20  # score >= 20 上报伦理委员会


@dataclass
class RiskCard:
    """单个风险的元数据卡。

    Attributes:
        risk_id: 风险唯一 ID
        title: 简短标题
        description: 详细描述 (场景 / 触发条件 / 影响)
        category: 风险类别
        severity: 严重度
        likelihood: 可能性
        mitigation_strategy: 缓解策略 (现状或计划)
        owner: 负责人
        status: 风险状态
        review_date: 下次 review 日期 (epoch)
        related_components: 相关组件 / 模块 (list)
        accepted_reason: 若 status=accepted,记录正式接受理由
        created_at: 创建时间
        updated_at: 最后更新时间
    """

    risk_id: str
    title: str
    description: str = ""
    category: RiskCategory = RiskCategory.OPERATIONAL
    severity: RiskSeverity = RiskSeverity.MEDIUM
    likelihood: RiskLikelihood = RiskLikelihood.UNLIKELY
    mitigation_strategy: str = ""
    owner: str = ""
    status: RiskStatus = RiskStatus.OPEN
    review_date: float = 0.0
    related_components: list[str] = field(default_factory=list)
    accepted_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        d["severity"] = self.severity.value
        d["likelihood"] = self.likelihood.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskCard":
        return cls(
            risk_id=data["risk_id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            category=RiskCategory(data.get("category", "operational")),
            severity=RiskSeverity(data.get("severity", "medium")),
            likelihood=RiskLikelihood(data.get("likelihood", "unlikely")),
            mitigation_strategy=data.get("mitigation_strategy", ""),
            owner=data.get("owner", ""),
            status=RiskStatus(data.get("status", "open")),
            review_date=float(data.get("review_date", 0.0)),
            related_components=list(data.get("related_components", [])),
            accepted_reason=data.get("accepted_reason", ""),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def compute_score(self) -> RiskScore:
        """基于 severity × likelihood 计算分数。"""
        return _compute_risk_score(self.severity, self.likelihood)


def _compute_risk_score(severity: RiskSeverity, likelihood: RiskLikelihood) -> RiskScore:
    """矩阵评分:score = severity_rank × likelihood_rank (1-25)。"""
    s = severity.rank()
    l = likelihood.rank()
    score = s * l
    if score >= ETHICS_COMMITTEE_THRESHOLD:
        level = "critical"
    elif score >= REVIEW_THRESHOLD:
        level = "high"
    elif score >= 10:
        level = "medium"
    else:
        level = "low"
    return RiskScore(
        score=score,
        severity=severity,
        likelihood=likelihood,
        requires_review=score >= REVIEW_THRESHOLD,
        requires_ethics_committee=score >= ETHICS_COMMITTEE_THRESHOLD,
        level=level,
    )


class RiskAssessment:
    """风险评估器 - 注册风险卡 + 矩阵评分 + 缓解跟踪。

    用法:
        ra = get_risk_assessment()
        card = RiskCard(risk_id="R-001", title="PII 泄漏", ...)
        ra.register(card)
        score = ra.assess_risk(RiskCategory.PRIVACY, RiskSeverity.HIGH, RiskLikelihood.LIKELY)
        if score.requires_review:
            ...
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or resolve_data_path("governance/risk_cards.json")
        self._lock = threading.RLock()
        self._cache: dict[str, RiskCard] = {}
        self._loaded = False

    def register(self, card: RiskCard) -> RiskCard:
        """注册 / 更新风险卡。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip risk register")
            return card
        with self._lock:
            self._load()
            card.updated_at = time.time()
            self._cache[card.risk_id] = card
            self._save()
            score = card.compute_score()
            logger.info(
                "Risk registered: %s (score=%d, level=%s)",
                card.risk_id, score.score, score.level,
            )
            if score.requires_ethics_committee:
                logger.warning(
                    "Risk %s requires ethics committee review (score=%d)",
                    card.risk_id, score.score,
                )
            return card

    def get(self, risk_id: str) -> Optional[RiskCard]:
        """按 ID 获取风险卡。"""
        with self._lock:
            self._load()
            return self._cache.get(risk_id)

    def list_all(self) -> list[RiskCard]:
        """列出所有风险卡。"""
        with self._lock:
            self._load()
            return list(self._cache.values())

    def list_by_severity(self, min_severity: RiskSeverity) -> list[RiskCard]:
        """列出严重度 >= min_severity 的风险卡。"""
        with self._lock:
            self._load()
            min_rank = min_severity.rank()
            return [
                c for c in self._cache.values()
                if c.severity.rank() >= min_rank
            ]

    def list_by_status(self, status: RiskStatus) -> list[RiskCard]:
        """按状态过滤。"""
        with self._lock:
            self._load()
            return [c for c in self._cache.values() if c.status == status]

    def list_by_category(self, category: RiskCategory) -> list[RiskCard]:
        """按类别过滤。"""
        with self._lock:
            self._load()
            return [c for c in self._cache.values() if c.category == category]

    def assess_risk(
        self,
        category: RiskCategory,
        severity: RiskSeverity,
        likelihood: RiskLikelihood,
    ) -> RiskScore:
        """矩阵评估 (不持久化,纯计算)。"""
        return _compute_risk_score(severity, likelihood)

    def mitigate(self, risk_id: str, strategy: str) -> Optional[RiskCard]:
        """记录缓解策略 + 状态切到 mitigating。"""
        with self._lock:
            self._load()
            card = self._cache.get(risk_id)
            if card is None:
                return None
            card.mitigation_strategy = strategy
            card.status = RiskStatus.MITIGATING
            card.updated_at = time.time()
            self._save()
            logger.info("Risk %s mitigating: %s", risk_id, strategy[:80])
            return card

    def accept(self, risk_id: str, reason: str) -> Optional[RiskCard]:
        """正式接受风险 (签字级别,需 reason)。"""
        if not reason or not reason.strip():
            raise ValueError("accept reason is required (formal acceptance)")
        with self._lock:
            self._load()
            card = self._cache.get(risk_id)
            if card is None:
                return None
            card.status = RiskStatus.ACCEPTED
            card.accepted_reason = reason
            card.updated_at = time.time()
            self._save()
            logger.info("Risk %s formally accepted: %s", risk_id, reason[:80])
            return card

    def close(self, risk_id: str) -> Optional[RiskCard]:
        """关闭风险 (缓解完成)。"""
        with self._lock:
            self._load()
            card = self._cache.get(risk_id)
            if card is None:
                return None
            card.status = RiskStatus.CLOSED
            card.updated_at = time.time()
            self._save()
            return card

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for cid, cdata in data.get("cards", {}).items():
                    self._cache[cid] = RiskCard.from_dict(cdata)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load risk cards failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "cards": {cid: c.to_dict() for cid, c in self._cache.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Save risk cards failed: %s", e)


# 全局单例
_ra_instance: Optional[RiskAssessment] = None
_ra_lock = threading.Lock()


def get_risk_assessment() -> RiskAssessment:
    global _ra_instance
    if _ra_instance is None:
        with _ra_lock:
            if _ra_instance is None:
                _ra_instance = RiskAssessment()
    return _ra_instance
