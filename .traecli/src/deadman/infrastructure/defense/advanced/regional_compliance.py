"""D20:区域化合规模块(Regional Compliance Unification)。

问题:
    deadman 现有合规分散在两个模块:
        - `compliance/data_residency.py`:DataRegion 枚举(CN/HK/US/EU/SG/GLOBAL)
        - `i18n/law_adapter.py`:Jurisdiction 枚举(CN_MAINLAND/CN_HONGKONG/US/EU/JP/KR/UK/OTHER)
    两者枚举不映射,接口割裂:
        - 调用方需同时查 DataResidency + LawAdapter,易遗漏
        - 数据驻留违规时,不会自动触发法律校验
        - 跨境时不会自动检查"目标 region 是否允许"
        - 无统一审计入口

    商业场景:
        - 用户在 CN_MAINLAND,数据要传到 US 服务商
        - 需同时检查:① 数据驻留是否允许 ② 跨境法律是否合规 ③ 是否需要用户同意
        - 现有需要调用方自己组合,容易出错

缓解:
    - RegionalComplianceOrchestrator:统一入口,组合 DataResidency + LawAdapter + Consent
    - region_to_jurisdiction / jurisdiction_to_region:枚举映射
    - check_cross_border:一站式跨境合规检查(驻留 + 法律 + 同意)
    - enforce_storage:存储前强制合规检查(透明拦截)
    - audit_compliance_event:统一审计入口

设计:
    orch = RegionalComplianceOrchestrator()
    # 1. 跨境检查
    result = orch.check_cross_border(
        tenant_id="t1",
        data_kind="personal_data",
        from_region="CN",
        to_region="US",
        user_id="u1",
    )
    if not result.allowed:
        raise ComplianceViolation(result.reason)

    # 2. 存储前检查
    orch.enforce_storage(
        tenant_id="t1",
        data_kind="sensitive_data",
        target_region="CN",
    )

集成:
    memory/file_store.py / vector_store / vault/store.py 存储前调用 enforce_storage。
    cross-border-specialist agent 调用 check_cross_border。

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


# =====================================================================
# 区域 / 司法辖区映射
# =====================================================================

class UnifiedRegion(str, Enum):
    """统一区域枚举(合并 DataRegion + Jurisdiction)。"""

    CN_MAINLAND = "CN_MAINLAND"
    CN_HONGKONG = "CN_HONGKONG"
    CN_TAIWAN = "CN_TAIWAN"  # 新增:台湾地区
    US = "US"
    EU = "EU"
    UK = "UK"  # 新增:脱欧后单独
    JP = "JP"
    KR = "KR"
    SG = "SG"
    GLOBAL = "GLOBAL"
    OTHER = "OTHER"


# DataRegion → UnifiedRegion 映射
_DATA_REGION_MAP = {
    "CN": UnifiedRegion.CN_MAINLAND,
    "HK": UnifiedRegion.CN_HONGKONG,
    "US": UnifiedRegion.US,
    "EU": UnifiedRegion.EU,
    "SG": UnifiedRegion.SG,
    "GLOBAL": UnifiedRegion.GLOBAL,
}

# Jurisdiction → UnifiedRegion 映射
_JURISDICTION_MAP = {
    "CN_MAINLAND": UnifiedRegion.CN_MAINLAND,
    "CN_HONGKONG": UnifiedRegion.CN_HONGKONG,
    "US": UnifiedRegion.US,
    "EU": UnifiedRegion.EU,
    "JP": UnifiedRegion.JP,
    "KR": UnifiedRegion.KR,
    "UK": UnifiedRegion.UK,
    "OTHER": UnifiedRegion.OTHER,
}


def data_region_to_unified(data_region: str) -> UnifiedRegion:
    """DataRegion 字符串 → UnifiedRegion。"""
    return _DATA_REGION_MAP.get(data_region, UnifiedRegion.OTHER)


def jurisdiction_to_unified(jurisdiction: str) -> UnifiedRegion:
    """Jurisdiction 字符串 → UnifiedRegion。"""
    return _JURISDICTION_MAP.get(jurisdiction, UnifiedRegion.OTHER)


def unified_to_data_region(region: UnifiedRegion) -> str:
    """反向映射:UnifiedRegion → DataRegion 字符串。"""
    reverse = {v: k for k, v in _DATA_REGION_MAP.items()}
    return reverse.get(region, "OTHER")


def unified_to_jurisdiction(region: UnifiedRegion) -> str:
    """反向映射:UnifiedRegion → Jurisdiction 字符串。"""
    reverse = {v: k for k, v in _JURISDICTION_MAP.items()}
    return reverse.get(region, "OTHER")


# =====================================================================
# 合规检查结果
# =====================================================================

class ComplianceLevel(str, Enum):
    """合规检查结果等级。"""

    ALLOWED = "allowed"
    ALLOWED_WITH_CONSENT = "allowed_with_consent"  # 需要用户同意
    ALLOWED_WITH_WARNING = "allowed_with_warning"  # 允许但有警告
    RESTRICTED = "restricted"  # 受限(需法务审核)
    FORBIDDEN = "forbidden"  # 禁止


@dataclass
class ComplianceCheckResult:
    """合规检查结果。"""

    allowed: bool
    level: ComplianceLevel
    region: UnifiedRegion
    data_kind: str
    # 详细信息
    residency_ok: bool = True
    legal_basis: str = ""
    cross_border_ok: bool = True
    consent_required: bool = False
    consent_obtained: bool = False
    warnings: list[str] = field(default_factory=list)
    # 法律依据(如 PIPL 第 38 条 / GDPR 第 44 条)
    legal_references: list[str] = field(default_factory=list)
    # 建议
    recommendations: list[str] = field(default_factory=list)
    # 检查时间
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "level": self.level.value,
            "region": self.region.value,
            "data_kind": self.data_kind,
            "residency_ok": self.residency_ok,
            "legal_basis": self.legal_basis,
            "cross_border_ok": self.cross_border_ok,
            "consent_required": self.consent_required,
            "consent_obtained": self.consent_obtained,
            "warnings": self.warnings,
            "legal_references": self.legal_references,
            "recommendations": self.recommendations,
            "checked_at": self.checked_at,
        }


class ComplianceViolation(Exception):
    """合规违规异常(强制阻止操作)。"""

    def __init__(self, result: ComplianceCheckResult) -> None:
        self.result = result
        super().__init__(
            f"Compliance violation: {result.level.value} for region={result.region.value}, "
            f"data_kind={result.data_kind}: {result.warnings}"
        )


# =====================================================================
# 数据分级
# =====================================================================

class DataKind(str, Enum):
    """数据分级(基于 GDPR / PIPL)。"""

    PUBLIC = "public"  # 公开数据
    GENERAL = "general"  # 一般数据(非 PII)
    PERSONAL = "personal"  # 个人数据(姓名 / 邮箱)
    SENSITIVE = "sensitive"  # 敏感数据(身份证 / 医疗 / 财务)
    SPECIAL = "special"  # 特殊数据(生物识别 / 宗教 / 政治倾向)


# 各 region 的数据驻留规则(简化)
_RESIDENCY_RULES = {
    UnifiedRegion.CN_MAINLAND: {
        "require_local_storage": True,  # PIPL:个人数据原则上境内存储
        "cross_border_allowed": False,  # 默认禁止跨境
        "cross_border_exceptions": ["consent", "legal_requirement", "contract_necessity"],
        "legal_basis": "PIPL 第 38 条",
        "sensitive_data_strict": True,  # 敏感数据额外严格
    },
    UnifiedRegion.CN_HONGKONG: {
        "require_local_storage": False,
        "cross_border_allowed": True,
        "cross_border_exceptions": [],
        "legal_basis": "PDPO 第 33 条",
        "sensitive_data_strict": True,
    },
    UnifiedRegion.US: {
        "require_local_storage": False,
        "cross_border_allowed": True,
        "cross_border_exceptions": [],
        "legal_basis": "State laws (CCPA / CPRA for California)",
        "sensitive_data_strict": False,
    },
    UnifiedRegion.EU: {
        "require_local_storage": False,
        "cross_border_allowed": True,  # 允许但需 adequacy decision / SCC
        "cross_border_exceptions": ["adequacy_decision", "scc", "bcr", "consent"],
        "legal_basis": "GDPR Chapter V",
        "sensitive_data_strict": True,
    },
    UnifiedRegion.GLOBAL: {
        "require_local_storage": False,
        "cross_border_allowed": True,
        "cross_border_exceptions": [],
        "legal_basis": "no restriction",
        "sensitive_data_strict": False,
    },
}


class RegionalComplianceOrchestrator:
    """区域化合规编排器。

    用法:
        orch = RegionalComplianceOrchestrator()
        # 跨境检查
        result = orch.check_cross_border(
            tenant_id="t1",
            data_kind="personal",
            from_region="CN",
            to_region="US",
            user_id="u1",
            consent_obtained=False,
        )
        if not result.allowed:
            raise ComplianceViolation(result)

        # 存储前检查
        orch.enforce_storage(
            tenant_id="t1",
            data_kind="sensitive",
            target_region="CN",
        )
    """

    def __init__(
        self,
        data_residency: Any = None,
        law_adapter: Any = None,
        consent_manager: Any = None,
    ) -> None:
        """延迟加载子模块(避免循环导入)。"""
        self._data_residency = data_residency
        self._law_adapter = law_adapter
        self._consent_manager = consent_manager
        self._lock = threading.RLock()
        # 审计事件
        self._audit_events: list[dict] = []

    def _get_data_residency(self):
        if self._data_residency is not None:
            return self._data_residency
        try:
            from ...compliance.data_residency import get_data_residency  # type: ignore[import-untyped]
            self._data_residency = get_data_residency()
        except ImportError:
            self._data_residency = None
        return self._data_residency

    def _get_law_adapter(self):
        if self._law_adapter is not None:
            return self._law_adapter
        try:
            from ...i18n.law_adapter import get_law_adapter  # type: ignore[import-untyped]
            self._law_adapter = get_law_adapter()
        except ImportError:
            self._law_adapter = None
        return self._law_adapter

    def _get_consent_manager(self):
        if self._consent_manager is not None:
            return self._consent_manager
        try:
            from ...compliance.consent import get_consent_manager  # type: ignore[import-untyped]
            self._consent_manager = get_consent_manager()
        except ImportError:
            self._consent_manager = None
        return self._consent_manager

    # ==================================================================
    # 跨境合规检查
    # ==================================================================

    def check_cross_border(
        self,
        *,
        tenant_id: str,
        data_kind: str,
        from_region: str,
        to_region: str,
        user_id: str | None = None,
        consent_obtained: bool = False,
        purpose: str = "",
    ) -> ComplianceCheckResult:
        """跨境合规检查(一站式)。

        检查:
            1. 数据驻留:from_region 是否允许数据出境
            2. 法律合规:from → to 跨境是否合法
            3. 用户同意:是否需要 + 是否获得
            4. 数据分级:敏感数据需额外检查

        Args:
            data_kind: 数据分级(public / general / personal / sensitive / special)
            from_region: 源区域(DataRegion 字符串或 UnifiedRegion)
            to_region: 目标区域
            consent_obtained: 是否已获用户同意
            purpose: 跨境目的(用于审计)
        """
        import time as _time
        from_unified = data_region_to_unified(from_region) if from_region in _DATA_REGION_MAP else (
            UnifiedRegion(from_region) if from_region in [r.value for r in UnifiedRegion] else UnifiedRegion.OTHER
        )
        to_unified = data_region_to_unified(to_region) if to_region in _DATA_REGION_MAP else (
            UnifiedRegion(to_region) if to_region in [r.value for r in UnifiedRegion] else UnifiedRegion.OTHER
        )

        result = ComplianceCheckResult(
            allowed=True,
            level=ComplianceLevel.ALLOWED,
            region=from_unified,
            data_kind=data_kind,
            checked_at=_time.time(),
        )

        if not is_enabled("defense"):
            return result

        # 1. 数据分级(同区域不涉及跨境)
        if from_unified == to_unified:
            # 同区域:检查数据驻留是否允许此 region 存储此 kind
            self._check_storage(result, from_unified, data_kind)
            return self._finalize(result, tenant_id, user_id, purpose)

        # 2. 跨境检查
        rules = _RESIDENCY_RULES.get(from_unified, _RESIDENCY_RULES[UnifiedRegion.GLOBAL])
        result.cross_border_ok = bool(rules.get("cross_border_allowed", True))

        # 法律依据
        result.legal_basis = str(rules.get("legal_basis", ""))
        if result.legal_basis:
            result.legal_references.append(result.legal_basis)

        # 是否需要用户同意
        exceptions_raw = rules.get("cross_border_exceptions", [])
        exceptions: list[str] = list(exceptions_raw) if isinstance(exceptions_raw, (list, tuple)) else []
        result.consent_required = "consent" in exceptions

        # 敏感数据额外严格
        if rules.get("sensitive_data_strict") and data_kind in ("sensitive", "special"):
            result.consent_required = True
            result.warnings.append(
                f"Sensitive data ({data_kind}) cross-border requires explicit consent"
            )

        # 检查用户同意状态
        if result.consent_required:
            if consent_obtained:
                result.consent_obtained = True
            else:
                # 询问 consent manager
                cm = self._get_consent_manager()
                if cm and user_id:
                    try:
                        has_consent = cm.has_consent(user_id, scope="cross_border")
                        result.consent_obtained = bool(has_consent)
                    except Exception as e:
                        logger.warning("Failed to check consent: %s", e)
                        result.consent_obtained = False

                if not result.consent_obtained:
                    result.cross_border_ok = False
                    result.allowed = False
                    result.level = ComplianceLevel.ALLOWED_WITH_CONSENT
                    result.warnings.append(
                        "Cross-border transfer requires user consent but not obtained"
                    )

        # 若跨境不允许且无例外 → 禁止
        if not result.cross_border_ok and not result.consent_obtained:
            result.allowed = False
            result.level = ComplianceLevel.FORBIDDEN

        # 调用 LawAdapter 进行详细法律校验
        la = self._get_law_adapter()
        if la:
            try:
                validation = la.validate_cross_border(
                    from_jurisdiction=unified_to_jurisdiction(from_unified),
                    to_jurisdiction=unified_to_jurisdiction(to_unified),
                    data_kind=data_kind,
                )
                # 兼容两种返回:dict 或 ValidationResult
                if hasattr(validation, "allowed"):
                    allowed = validation.allowed
                    requires_consent = getattr(validation, "requires_consent", False) or getattr(validation, "consents_required", [])
                    warnings = getattr(validation, "warnings", [])
                    legal_basis = getattr(validation, "legal_basis", "")
                elif isinstance(validation, dict):
                    allowed = validation.get("allowed", True)
                    requires_consent = validation.get("requires_consent", False) or validation.get("consents_required", [])
                    warnings = validation.get("warnings", [])
                    legal_basis = validation.get("legal_basis", "")
                else:
                    allowed = True
                    requires_consent = False
                    warnings = []
                    legal_basis = ""

                if not allowed:
                    result.cross_border_ok = False
                    result.allowed = False
                    result.level = ComplianceLevel.FORBIDDEN
                if requires_consent and not result.consent_obtained:
                    result.consent_required = True
                    if not result.consent_obtained:
                        result.allowed = False
                        result.level = ComplianceLevel.ALLOWED_WITH_CONSENT
                if warnings:
                    result.warnings.extend(warnings)
                if legal_basis:
                    result.legal_references.append(legal_basis)
            except Exception as e:
                logger.warning("LawAdapter validation failed: %s", e)
                result.warnings.append(f"Legal validation skipped: {e}")

        # 推荐建议
        if result.consent_required and not result.consent_obtained:
            result.recommendations.append("Obtain explicit user consent before cross-border transfer")
        if data_kind == "sensitive":
            result.recommendations.append("Consider data anonymization before transfer")
        if from_unified == UnifiedRegion.CN_MAINLAND:
            result.recommendations.append("Conduct PIPL Article 38 security assessment")

        return self._finalize(result, tenant_id, user_id, purpose)

    # ==================================================================
    # 存储前检查
    # ==================================================================

    def enforce_storage(
        self,
        *,
        tenant_id: str,
        data_kind: str,
        target_region: str,
        user_id: str | None = None,
    ) -> ComplianceCheckResult:
        """存储前合规检查(透明拦截)。"""
        import time as _time
        region = data_region_to_unified(target_region) if target_region in _DATA_REGION_MAP else (
            UnifiedRegion(target_region) if target_region in [r.value for r in UnifiedRegion] else UnifiedRegion.OTHER
        )
        result = ComplianceCheckResult(
            allowed=True,
            level=ComplianceLevel.ALLOWED,
            region=region,
            data_kind=data_kind,
            checked_at=_time.time(),
        )
        if not is_enabled("defense"):
            return result

        self._check_storage(result, region, data_kind)

        # 若不允许,抛异常(强制阻止)
        if not result.allowed:
            self._finalize(result, tenant_id, user_id, "storage")
            raise ComplianceViolation(result)

        return self._finalize(result, tenant_id, user_id, "storage")

    def _check_storage(
        self,
        result: ComplianceCheckResult,
        region: UnifiedRegion,
        data_kind: str,
    ) -> None:
        """检查 region 是否允许存储此 kind 的数据。"""
        rules = _RESIDENCY_RULES.get(region, _RESIDENCY_RULES[UnifiedRegion.GLOBAL])
        if rules.get("require_local_storage") and data_kind in ("sensitive", "special"):
            # 仅允许本地存储敏感数据
            result.residency_ok = True
            result.warnings.append(
                f"Sensitive data must remain in {region.value} (local storage required)"
            )
        # 全局区域不允许存储敏感数据
        if region == UnifiedRegion.GLOBAL and data_kind in ("sensitive", "special"):
            result.residency_ok = False
            result.allowed = False
            result.level = ComplianceLevel.FORBIDDEN
            result.warnings.append(
                "Cannot store sensitive data in GLOBAL region (no data residency protection)"
            )

    # ==================================================================
    # 审计
    # ==================================================================

    def _finalize(
        self,
        result: ComplianceCheckResult,
        tenant_id: str,
        user_id: str | None,
        purpose: str,
    ) -> ComplianceCheckResult:
        """记录审计事件并返回结果。"""
        with self._lock:
            self._audit_events.append({
                "tenant_id": tenant_id,
                "user_id": user_id,
                "purpose": purpose,
                "result": result.to_dict(),
            })
            if len(self._audit_events) > 10000:
                self._audit_events = self._audit_events[-5000:]
        return result

    def list_audit_events(
        self,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._lock:
            events = list(self._audit_events)
        if tenant_id:
            events = [e for e in events if e["tenant_id"] == tenant_id]
        return events[-limit:]


# =====================================================================
# 全局单例
# =====================================================================

_orchestrator: RegionalComplianceOrchestrator | None = None
_lock = threading.Lock()


def get_regional_compliance_orchestrator() -> RegionalComplianceOrchestrator:
    global _orchestrator
    with _lock:
        if _orchestrator is None:
            _orchestrator = RegionalComplianceOrchestrator()
        return _orchestrator


def reset_regional_compliance_orchestrator() -> None:
    global _orchestrator
    with _lock:
        _orchestrator = None
