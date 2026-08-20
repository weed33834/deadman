"""P8.20 AI 责任保险 - 记录 AI 决策错误的责任归属与赔付台账。

借鉴自动驾驶责任险 (英国 AV Act 2018 + 自动驾驶责任保险) 模式,
为 AI 决策错误 (错误法律建议 / 误导性内容 / 数据泄露损失) 建立保险台账。

模块结构:
    - InsurancePolicy: 保险单 (dataclass)
    - InsuranceClaim: 理赔案 (dataclass)
    - LiabilityInsurance: 保险管理器 (注册保单 / 受理理赔 / 处理 / 预检)

设计:
    - 纯记录 (record-keeping),无真实保险 API
    - check_coverage(incident_type, amount) 用于关键动作前预检
      (例:转账前检查是否在保险覆盖范围内)
    - 持久化到 data/governance/liability_insurance.json (按租户隔离)
    - 原子写 + 线程安全

feature flag:`DEADMAN_GOVERNANCE_ENABLED=0` 关闭时 check_coverage 返回 False。
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


# 标准保险覆盖类型 (借鉴 AV Act + AI 责任险方案)
class CoverageType(str, Enum):
    """AI 责任险覆盖类型。"""

    LEGAL_ADVICE_ERROR = "legal_advice_error"  # 错误法律建议
    DATA_LEAK = "data_leak"  # 数据泄露损失
    IP_INFRINGEMENT = "ip_infringement"  # 知识产权侵权
    BIAS_DISCRIMINATION = "bias_discrimination"  # 算法歧视
    MISLEADING_CONTENT = "misleading_content"  # 误导性内容
    SERVICE_INTERRUPTION = "service_interruption"  # 服务中断
    THIRD_PARTY_DAMAGE = "third_party_damage"  # 第三方损害


class ClaimStatus(str, Enum):
    """理赔状态机:

    FILED → UNDER_REVIEW → APPROVED → PAID
                       ↓
                  REJECTED
    """

    FILED = "filed"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"  # 已赔付


@dataclass
class InsurancePolicy:
    """保险单。

    Attributes:
        policy_id: 保单 ID
        provider: 保险公司
        coverage_amount: 总保额 (元)
        deductible: 免赔额 (元)
        coverage_types: 覆盖类型 (list of CoverageType value strings)
        exclusions: 排除条款 (list of strings)
        start_date: 保单起始 (epoch)
        end_date: 保单终止 (epoch)
        premium: 保费 (元)
        active: 是否生效
    """

    policy_id: str
    provider: str
    coverage_amount: float = 0.0
    deductible: float = 0.0
    coverage_types: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    start_date: float = 0.0
    end_date: float = 0.0
    premium: float = 0.0
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InsurancePolicy:
        return cls(
            policy_id=data["policy_id"],
            provider=data.get("provider", ""),
            coverage_amount=float(data.get("coverage_amount", 0.0)),
            deductible=float(data.get("deductible", 0.0)),
            coverage_types=list(data.get("coverage_types", [])),
            exclusions=list(data.get("exclusions", [])),
            start_date=float(data.get("start_date", 0.0)),
            end_date=float(data.get("end_date", 0.0)),
            premium=float(data.get("premium", 0.0)),
            active=bool(data.get("active", True)),
        )

    def is_valid_at(self, ts: float | None = None) -> bool:
        """保单在 ts 时刻是否有效 (active + 时间范围内)。"""
        if not self.active:
            return False
        current = ts if ts is not None else time.time()
        if self.start_date and current < self.start_date:
            return False
        return not (self.end_date and current > self.end_date)


@dataclass
class InsuranceClaim:
    """理赔案。

    Attributes:
        claim_id: 理赔 ID
        policy_id: 关联保单 ID
        user_id: 申请人 ID
        incident_description: 事件描述
        amount_claimed: 索赔金额 (元)
        status: 理赔状态
        filed_at: 提交时间
        resolved_at: 结案时间
        payout_amount: 赔付金额 (元,实际赔付)
        coverage_type: 覆盖类型
        reviewer_id: 审核人
        resolution_text: 结案说明
    """

    claim_id: str
    policy_id: str
    user_id: str
    incident_description: str = ""
    amount_claimed: float = 0.0
    status: ClaimStatus = ClaimStatus.FILED
    filed_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    payout_amount: float = 0.0
    coverage_type: str = ""
    reviewer_id: str = ""
    resolution_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InsuranceClaim:
        return cls(
            claim_id=data["claim_id"],
            policy_id=data["policy_id"],
            user_id=data["user_id"],
            incident_description=data.get("incident_description", ""),
            amount_claimed=float(data.get("amount_claimed", 0.0)),
            status=ClaimStatus(data.get("status", "filed")),
            filed_at=float(data.get("filed_at", time.time())),
            resolved_at=data.get("resolved_at"),
            payout_amount=float(data.get("payout_amount", 0.0)),
            coverage_type=data.get("coverage_type", ""),
            reviewer_id=data.get("reviewer_id", ""),
            resolution_text=data.get("resolution_text", ""),
        )


class LiabilityInsurance:
    """AI 责任保险管理器 (record-keeping only)。

    用法:
        li = get_liability_insurance()
        li.register_policy(InsurancePolicy(...))
        if li.check_coverage("data_leak", 100000):
            # 在保险覆盖内,可执行有风险的操作
            ...
        claim = li.file_claim({"policy_id": "P-001", "user_id": "u1", ...})
        li.process_claim(claim.claim_id, ClaimStatus.APPROVED, 80000)
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or resolve_data_path("governance/liability_insurance.json")
        self._lock = threading.RLock()
        self._policies: dict[str, InsurancePolicy] = {}
        self._claims: dict[str, InsuranceClaim] = {}
        self._loaded = False
        self._counter = 0

    # ==================================================================
    # 保单管理
    # ==================================================================

    def register_policy(self, policy: InsurancePolicy) -> InsurancePolicy:
        """注册保单。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip policy register")
            return policy
        with self._lock:
            self._load()
            self._policies[policy.policy_id] = policy
            self._save()
            logger.info(
                "Insurance policy registered: %s (provider=%s amount=%s)",
                policy.policy_id,
                policy.provider,
                policy.coverage_amount,
            )
            return policy

    def get_policy(self, policy_id: str) -> InsurancePolicy | None:
        with self._lock:
            self._load()
            return self._policies.get(policy_id)

    def list_policies(self, active_only: bool = False) -> list[InsurancePolicy]:
        with self._lock:
            self._load()
            policies = list(self._policies.values())
            if active_only:
                policies = [p for p in policies if p.is_valid_at()]
            return policies

    def get_coverage(self, policy_id: str) -> dict[str, Any]:
        """获取保单覆盖摘要。"""
        with self._lock:
            self._load()
            policy = self._policies.get(policy_id)
            if policy is None:
                return {
                    "policy_id": policy_id,
                    "found": False,
                    "active": False,
                }
            # 统计该保单下已理赔金额
            total_claimed = sum(
                c.amount_claimed for c in self._claims.values() if c.policy_id == policy_id
            )
            total_paid = sum(
                c.payout_amount for c in self._claims.values() if c.policy_id == policy_id
            )
            remaining = max(0.0, policy.coverage_amount - total_paid)
            return {
                "policy_id": policy_id,
                "found": True,
                "active": policy.is_valid_at(),
                "provider": policy.provider,
                "coverage_amount": policy.coverage_amount,
                "deductible": policy.deductible,
                "coverage_types": list(policy.coverage_types),
                "exclusions": list(policy.exclusions),
                "total_claimed": total_claimed,
                "total_paid": total_paid,
                "remaining_coverage": remaining,
                "valid": policy.is_valid_at(),
            }

    def check_coverage(self, incident_type: str, amount: float) -> bool:
        """关键动作前预检 - 是否有保单覆盖该风险。

        Args:
            incident_type: 事件类型 (CoverageType.value 或自定义)
            amount: 涉及金额

        Returns:
            True if 至少一张有效保单覆盖该 incident_type 且剩余保额 >= amount
        """
        if not is_enabled("governance"):
            logger.debug("Governance disabled, coverage check returns False")
            return False
        with self._lock:
            self._load()
            for policy in self._policies.values():
                if not policy.is_valid_at():
                    continue
                if incident_type not in policy.coverage_types:
                    continue
                # 检查 exclusions 是否包含 incident_type
                if any(incident_type in exc or exc in incident_type for exc in policy.exclusions):
                    continue
                # 检查剩余保额
                summary = self.get_coverage(policy.policy_id)
                if summary["remaining_coverage"] >= amount:
                    return True
            return False

    # ==================================================================
    # 理赔管理
    # ==================================================================

    def file_claim(self, claim_data: dict[str, Any]) -> InsuranceClaim:
        """受理理赔申请。

        Args:
            claim_data: dict,至少包含 policy_id / user_id,可选 incident_description /
                       amount_claimed / coverage_type

        Returns:
            创建的 InsuranceClaim
        """
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip claim file")
            return InsuranceClaim(
                claim_id="disabled",
                policy_id=claim_data.get("policy_id", ""),
                user_id=claim_data.get("user_id", ""),
            )
        with self._lock:
            self._load()
            policy_id = claim_data.get("policy_id", "")
            if policy_id not in self._policies:
                raise ValueError(f"Policy not found: {policy_id}")
            claim_id = self._generate_id()
            claim = InsuranceClaim(
                claim_id=claim_id,
                policy_id=policy_id,
                user_id=claim_data.get("user_id", ""),
                incident_description=claim_data.get("incident_description", ""),
                amount_claimed=float(claim_data.get("amount_claimed", 0.0)),
                coverage_type=claim_data.get("coverage_type", ""),
                status=ClaimStatus.FILED,
            )
            self._claims[claim_id] = claim
            self._save()
            logger.info(
                "Insurance claim filed: %s (policy=%s amount=%s)",
                claim_id,
                policy_id,
                claim.amount_claimed,
            )
            return claim

    def process_claim(
        self,
        claim_id: str,
        decision: ClaimStatus,
        payout: float = 0.0,
        reviewer_id: str = "",
        resolution_text: str = "",
    ) -> InsuranceClaim:
        """处理理赔 (审核决定 + 赔付金额)。"""
        with self._lock:
            self._load()
            claim = self._claims.get(claim_id)
            if claim is None:
                raise KeyError(f"Claim not found: {claim_id}")
            if not isinstance(decision, ClaimStatus):
                raise ValueError(f"decision must be ClaimStatus, got {type(decision)}")
            if decision == ClaimStatus.APPROVED:
                claim.status = ClaimStatus.APPROVED
                claim.payout_amount = float(payout)
            elif decision == ClaimStatus.REJECTED:
                claim.status = ClaimStatus.REJECTED
            elif decision == ClaimStatus.PAID:
                claim.status = ClaimStatus.PAID
                claim.payout_amount = float(payout)
            elif decision == ClaimStatus.UNDER_REVIEW:
                claim.status = ClaimStatus.UNDER_REVIEW
            else:
                raise ValueError(f"Unsupported decision: {decision}")
            claim.reviewer_id = reviewer_id
            claim.resolution_text = resolution_text
            claim.resolved_at = time.time()
            self._save()
            logger.info(
                "Insurance claim %s processed: %s payout=%s",
                claim_id,
                decision.value,
                payout,
            )
            return claim

    def get_claim(self, claim_id: str) -> InsuranceClaim | None:
        with self._lock:
            self._load()
            return self._claims.get(claim_id)

    def list_claims(
        self,
        status: ClaimStatus | None = None,
        policy_id: str | None = None,
    ) -> list[InsuranceClaim]:
        with self._lock:
            self._load()
            claims = list(self._claims.values())
            if status:
                claims = [c for c in claims if c.status == status]
            if policy_id:
                claims = [c for c in claims if c.policy_id == policy_id]
            claims.sort(key=lambda c: c.filed_at, reverse=True)
            return claims

    # ==================================================================
    # 内部
    # ==================================================================

    def _generate_id(self) -> str:
        self._counter += 1
        return f"claim-{int(time.time())}-{self._counter}"

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for pid, pdata in data.get("policies", {}).items():
                    self._policies[pid] = InsurancePolicy.from_dict(pdata)
                for cid, cdata in data.get("claims", {}).items():
                    self._claims[cid] = InsuranceClaim.from_dict(cdata)
                self._counter = int(data.get("counter", 0))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load liability insurance failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "counter": self._counter,
                "policies": {pid: p.to_dict() for pid, p in self._policies.items()},
                "claims": {cid: c.to_dict() for cid, c in self._claims.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Save liability insurance failed: %s", e)


# 全局单例
_li_instance: LiabilityInsurance | None = None
_li_lock = threading.Lock()


def get_liability_insurance() -> LiabilityInsurance:
    global _li_instance
    if _li_instance is None:
        with _li_lock:
            if _li_instance is None:
                _li_instance = LiabilityInsurance()
    return _li_instance
