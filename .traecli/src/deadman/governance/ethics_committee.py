"""P8.17 AI 治理框架 - 伦理委员会审查 (Ethics Committee Review)。

借鉴医院伦理委员会 (IRB) + Anthropic Ethics Board 模式,
针对高风险 AI 决策 (数字孪生逝者 / 重大伦理冲突) 召集伦理委员会审议,
强制 quorum (主席 + 律师 + 至少 3 人),出具书面决定。

模块结构:
    - CommitteeMember: 委员 (dataclass)
    - EthicsCase: 伦理案件 (dataclass)
    - EthicsCommittee: 委员会管理器 (注册委员 / 提案 / 分配 / 决议)

设计:
    - 委员角色:chair (主席) / lawyer (律师) / ethicist (伦理学者) /
                engineer (工程师) / user_rep (用户代表)
    - quorum:高严重度案件须 >= 3 人含 chair + lawyer
    - 数字孪生逝者 (digital_twin_deceased) 类案件须 user_rep + 用户同意验证
    - 持久化到 data/governance/ethics_cases.json (按租户隔离)
    - 原子写 + 线程安全
    - 纯记录 (无法律效力,但作为治理证据)

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


class MemberRole(str, Enum):
    """委员角色。"""

    CHAIR = "chair"  # 主席 (召集 + 仲裁)
    LAWYER = "lawyer"  # 律师 (法律合规)
    ETHICIST = "ethicist"  # 伦理学者
    ENGINEER = "engineer"  # 工程师 (技术可行性)
    USER_REP = "user_rep"  # 用户代表 (用户权益)


class CaseStatus(str, Enum):
    """案件状态机:

    SUBMITTED → ASSIGNED → DECIDED
                      ↓
                 WITHDRAWN (撤回)
    """

    SUBMITTED = "submitted"
    ASSIGNED = "assigned"
    DECIDED = "decided"
    WITHDRAWN = "withdrawn"


class CaseDecision(str, Enum):
    """决议结果。"""

    APPROVED = "approved"  # 批准 (可执行)
    REJECTED = "rejected"  # 驳回 (不可执行)
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"  # 有条件批准
    DEFERRED = "deferred"  # 暂缓 (需补充材料)


@dataclass
class CommitteeMember:
    """委员会成员。

    Attributes:
        member_id: 委员 ID
        name: 姓名
        role: 角色 (chair / lawyer / ethicist / engineer / user_rep)
        expertise: 专长领域 (list)
        active: 是否在职
    """

    member_id: str
    name: str
    role: MemberRole
    expertise: list[str] = field(default_factory=list)
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["role"] = self.role.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommitteeMember":
        return cls(
            member_id=data["member_id"],
            name=data.get("name", ""),
            role=MemberRole(data.get("role", "engineer")),
            expertise=list(data.get("expertise", [])),
            active=bool(data.get("active", True)),
        )


@dataclass
class EthicsCase:
    """伦理案件。

    Attributes:
        case_id: 案件 ID
        title: 标题
        description: 详细描述
        category: 案件类别 (如 digital_twin_deceased / bias_dispute / content_removal)
        severity: 严重度 (low / medium / high)
        status: 案件状态
        submitted_at: 提案时间
        assigned_members: 分配委员 ID (list)
        decision: 决议结果 (None=未决)
        decision_text: 决议说明
        decision_date: 决议日期
        user_consent_verified: 用户同意是否已验证 (数字孪生逝者案件强制)
        metadata: 附加元数据
    """

    case_id: str
    title: str
    description: str = ""
    category: str = ""
    severity: str = "medium"  # low / medium / high
    status: CaseStatus = CaseStatus.SUBMITTED
    submitted_at: float = field(default_factory=time.time)
    assigned_members: list[str] = field(default_factory=list)
    decision: Optional[CaseDecision] = None
    decision_text: str = ""
    decision_date: Optional[float] = None
    user_consent_verified: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["decision"] = self.decision.value if self.decision else None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EthicsCase":
        decision_val = data.get("decision")
        decision = None
        if decision_val:
            try:
                decision = CaseDecision(decision_val)
            except ValueError:
                decision = None
        return cls(
            case_id=data["case_id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            category=data.get("category", ""),
            severity=data.get("severity", "medium"),
            status=CaseStatus(data.get("status", "submitted")),
            submitted_at=float(data.get("submitted_at", time.time())),
            assigned_members=list(data.get("assigned_members", [])),
            decision=decision,
            decision_text=data.get("decision_text", ""),
            decision_date=data.get("decision_date"),
            user_consent_verified=bool(data.get("user_consent_verified", False)),
            metadata=dict(data.get("metadata", {})),
        )


# 数字孪生逝者案件类别
DIGITAL_TWIN_DECEASED_CATEGORY = "digital_twin_deceased"


class EthicsCommittee:
    """伦理委员会 - 注册委员 / 提案 / 分配 / 决议。

    用法:
        ec = get_ethics_committee()
        ec.register_member(CommitteeMember(member_id="m1", name="张三", role=MemberRole.CHAIR))
        case = ec.submit_case("数字孪生逝者", "用户授权...", "digital_twin_deceased", "high")
        ec.assign(case.case_id, ["m1", "m2", "m3"])
        ec.decide(case.case_id, CaseDecision.APPROVED, "已审议通过")
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or resolve_data_path("governance/ethics_cases.json")
        self._lock = threading.RLock()
        self._members: dict[str, CommitteeMember] = {}
        self._cases: dict[str, EthicsCase] = {}
        self._loaded = False
        self._counter = 0

    # ==================================================================
    # 委员管理
    # ==================================================================

    def register_member(self, member: CommitteeMember) -> CommitteeMember:
        """注册委员。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip member register")
            return member
        with self._lock:
            self._load()
            self._members[member.member_id] = member
            self._save()
            logger.info("Committee member registered: %s (%s)", member.member_id, member.role.value)
            return member

    def list_members(self, role: Optional[MemberRole] = None) -> list[CommitteeMember]:
        """列出委员 (可按 role 过滤)。"""
        with self._lock:
            self._load()
            members = [m for m in self._members.values() if m.active]
            if role:
                members = [m for m in members if m.role == role]
            return members

    def get_member(self, member_id: str) -> Optional[CommitteeMember]:
        with self._lock:
            self._load()
            return self._members.get(member_id)

    # ==================================================================
    # 案件管理
    # ==================================================================

    def submit_case(
        self,
        title: str,
        description: str,
        category: str,
        severity: str = "medium",
    ) -> EthicsCase:
        """提交伦理案件。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip case submit")
            return EthicsCase(
                case_id="disabled",
                title=title,
                description=description,
                category=category,
                severity=severity,
                status=CaseStatus.SUBMITTED,
            )
        with self._lock:
            self._load()
            case_id = self._generate_id()
            case = EthicsCase(
                case_id=case_id,
                title=title,
                description=description,
                category=category,
                severity=severity,
                status=CaseStatus.SUBMITTED,
            )
            self._cases[case_id] = case
            self._save()
            logger.info(
                "Ethics case submitted: %s (category=%s severity=%s)",
                case_id, category, severity,
            )
            return case

    def assign(self, case_id: str, member_ids: list[str]) -> EthicsCase:
        """分配委员到案件 + quorum 校验。"""
        with self._lock:
            self._load()
            case = self._cases.get(case_id)
            if case is None:
                raise KeyError(f"Ethics case not found: {case_id}")

            # 校验委员存在
            for mid in member_ids:
                if mid not in self._members:
                    raise ValueError(f"Member not found: {mid}")

            # quorum 校验
            self._check_quorum(case, member_ids)

            # 数字孪生逝者案件强制 user_rep + 用户同意验证
            if case.category == DIGITAL_TWIN_DECEASED_CATEGORY:
                self._check_digital_twin_requirements(case, member_ids)

            case.assigned_members = list(member_ids)
            case.status = CaseStatus.ASSIGNED
            self._save()
            logger.info(
                "Ethics case %s assigned to %d members", case_id, len(member_ids),
            )
            return case

    def decide(
        self,
        case_id: str,
        decision: CaseDecision,
        decision_text: str,
    ) -> EthicsCase:
        """出具决议。"""
        with self._lock:
            self._load()
            case = self._cases.get(case_id)
            if case is None:
                raise KeyError(f"Ethics case not found: {case_id}")
            if case.status != CaseStatus.ASSIGNED:
                raise ValueError(
                    f"Case {case_id} cannot be decided in status={case.status.value}"
                )
            if not isinstance(decision, CaseDecision):
                raise ValueError(f"decision must be CaseDecision, got {type(decision)}")
            case.decision = decision
            case.decision_text = decision_text
            case.decision_date = time.time()
            case.status = CaseStatus.DECIDED
            self._save()
            logger.info(
                "Ethics case %s decided: %s", case_id, decision.value,
            )
            return case

    def list_cases(
        self,
        status: Optional[CaseStatus] = None,
        category: Optional[str] = None,
    ) -> list[EthicsCase]:
        """列出案件 (可按 status / category 过滤)。"""
        with self._lock:
            self._load()
            cases = list(self._cases.values())
            if status:
                cases = [c for c in cases if c.status == status]
            if category:
                cases = [c for c in cases if c.category == category]
            cases.sort(key=lambda c: c.submitted_at, reverse=True)
            return cases

    def get(self, case_id: str) -> Optional[EthicsCase]:
        with self._lock:
            self._load()
            return self._cases.get(case_id)

    def verify_user_consent(self, case_id: str) -> EthicsCase:
        """标记用户同意已验证 (数字孪生逝者案件必须)。"""
        with self._lock:
            self._load()
            case = self._cases.get(case_id)
            if case is None:
                raise KeyError(f"Ethics case not found: {case_id}")
            case.user_consent_verified = True
            self._save()
            return case

    def withdraw(self, case_id: str) -> EthicsCase:
        """撤回案件。"""
        with self._lock:
            self._load()
            case = self._cases.get(case_id)
            if case is None:
                raise KeyError(f"Ethics case not found: {case_id}")
            case.status = CaseStatus.WITHDRAWN
            self._save()
            return case

    # ==================================================================
    # quorum 校验
    # ==================================================================

    def _check_quorum(self, case: EthicsCase, member_ids: list[str]) -> None:
        """quorum 校验:
        - 低 / 中严重度:至少 1 人
        - 高严重度:至少 3 人含 chair + lawyer
        """
        if case.severity not in ("low", "medium", "high"):
            raise ValueError(f"Invalid severity: {case.severity}")

        if case.severity in ("low", "medium"):
            if len(member_ids) < 1:
                raise ValueError("Quorum not met: at least 1 member required")
            return

        # high severity
        if len(member_ids) < 3:
            raise ValueError(
                "Quorum not met: high-severity case requires at least 3 members"
            )
        roles_present = set()
        for mid in member_ids:
            member = self._members.get(mid)
            if member:
                roles_present.add(member.role)
        if MemberRole.CHAIR not in roles_present:
            raise ValueError(
                "Quorum not met: high-severity case requires chair"
            )
        if MemberRole.LAWYER not in roles_present:
            raise ValueError(
                "Quorum not met: high-severity case requires lawyer"
            )

    def _check_digital_twin_requirements(
        self,
        case: EthicsCase,
        member_ids: list[str],
    ) -> None:
        """数字孪生逝者案件强制:user_rep 在席 + 用户同意已验证。"""
        roles_present = set()
        for mid in member_ids:
            member = self._members.get(mid)
            if member:
                roles_present.add(member.role)
        if MemberRole.USER_REP not in roles_present:
            raise ValueError(
                "Digital twin deceased case requires user_rep in committee"
            )
        if not case.user_consent_verified:
            raise ValueError(
                "Digital twin deceased case requires verified user consent "
                "(call verify_user_consent first)"
            )

    # ==================================================================
    # 内部
    # ==================================================================

    def _generate_id(self) -> str:
        self._counter += 1
        return f"ethics-{int(time.time())}-{self._counter}"

    # ==================================================================
    # 持久化
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for mid, mdata in data.get("members", {}).items():
                    self._members[mid] = CommitteeMember.from_dict(mdata)
                for cid, cdata in data.get("cases", {}).items():
                    self._cases[cid] = EthicsCase.from_dict(cdata)
                self._counter = int(data.get("counter", 0))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load ethics cases failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "counter": self._counter,
                "members": {mid: m.to_dict() for mid, m in self._members.items()},
                "cases": {cid: c.to_dict() for cid, c in self._cases.items()},
            }
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, self.store_path)
        except OSError as e:
            logger.error("Save ethics cases failed: %s", e)


# 全局单例
_ec_instance: Optional[EthicsCommittee] = None
_ec_lock = threading.Lock()


def get_ethics_committee() -> EthicsCommittee:
    global _ec_instance
    if _ec_instance is None:
        with _ec_lock:
            if _ec_instance is None:
                _ec_instance = EthicsCommittee()
    return _ec_instance
