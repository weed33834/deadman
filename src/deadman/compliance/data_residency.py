"""P8.6.1 数据驻留 - 数据不出境(PIPL / GDPR 合规)。

法规要求:
    - PIPL 第 38-43 条:个人信息出境需用户单独同意 + 安全评估
    - GDPR 第 44-50 条:跨境传输需标准合同条款 / 充分性认定
    - 数据安全法第 31 条:重要数据出境需国家网信办评估

设计:
    - DataRegion: 枚举数据所在地理区域(CN / US / EU / SG)
    - DataResidency: 驻留策略(用户指定 region,数据不可出 region)
    - 跨境检查:写入 / 读取 / 传输时校验 region 一致性
    - 异常:违规跨境抛 ResidencyViolation

feature flag:`DEADMAN_COMPLIANCE_ENABLED=0` 关闭时不校验(透传)
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


class DataRegion(str, Enum):
    """数据驻留区域。"""

    CN = "cn"  # 中国大陆
    HK = "hk"  # 香港(特别行政区,数据策略特殊)
    US = "us"  # 美国
    EU = "eu"  # 欧盟
    SG = "sg"  # 新加坡
    GLOBAL = "global"  # 全球(不限制)


class ResidencyViolation(Exception):
    """数据驻留违规(跨境传输 / 跨租户 / 跨用户)。"""

    def __init__(self, msg: str, *, src_region: str, dst_region: str, data_kind: str = "") -> None:
        self.src_region = src_region
        self.dst_region = dst_region
        self.data_kind = data_kind
        super().__init__(msg)


@dataclass
class ResidencyPolicy:
    """单租户的驻留策略。"""

    tenant_id: str
    primary_region: DataRegion  # 主存储区域
    allowed_regions: list[DataRegion] = field(default_factory=list)  # 允许访问的区域
    cross_border_consent: bool = False  # 是否已获跨境同意
    cross_border_consent_at: float | None = None
    # 数据分类(按敏感度)
    sensitive_data_regions: dict[str, DataRegion] = field(default_factory=dict)
    # key = data_kind(user_profile / chat_history / financial / legal_doc)


@dataclass
class ExportResult:
    """受控跨境导出结果。"""

    success: bool
    user_id: str
    target_region: str
    data_kind: str
    audited: bool
    reason: str = ""


class DataResidency:
    """数据驻留管理器。

    持久化:`data/compliance/residency.yaml`(每个租户一个策略)
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_RESIDENCY_STORE", "data/compliance/residency.yaml")
        )
        self._lock = threading.RLock()
        self._policies: dict[str, ResidencyPolicy] = {}
        self._loaded = False

    # ==================================================================
    # 驻留策略管理
    # ==================================================================

    def set_policy(
        self,
        tenant_id: str,
        primary_region: str,
        allowed_regions: list[str] | None = None,
        cross_border_consent: bool = False,
        sensitive_data_regions: dict[str, str] | None = None,
    ) -> ResidencyPolicy:
        """设置租户的驻留策略。

        Args:
            tenant_id: 租户 ID(默认走 multi_tenant.get_current_tenant_id())
            primary_region: 主存储区域(cn / us / eu / hk / sg / global)
            allowed_regions: 允许访问的其他区域列表
            cross_border_consent: 是否已获跨境同意
            sensitive_data_regions: 敏感数据专属区域
                {data_kind: region} 例:{"financial": "cn", "legal_doc": "cn"}
        """
        if not is_enabled("compliance"):
            # 关闭:返回 GLOBAL(无限制)
            return ResidencyPolicy(
                tenant_id=tenant_id,
                primary_region=DataRegion.GLOBAL,
                allowed_regions=[DataRegion.GLOBAL],
                cross_border_consent=True,
            )

        with self._lock:
            self._load()
            sensitive_map: dict[str, DataRegion] = {}
            if sensitive_data_regions:
                for kind, region in sensitive_data_regions.items():
                    sensitive_map[kind] = DataRegion(region)
            policy = ResidencyPolicy(
                tenant_id=tenant_id,
                primary_region=DataRegion(primary_region),
                allowed_regions=[DataRegion(r) for r in (allowed_regions or [primary_region])],
                cross_border_consent=cross_border_consent,
                cross_border_consent_at=__import__("time").time() if cross_border_consent else None,
                sensitive_data_regions=sensitive_map,
            )
            self._policies[tenant_id] = policy
            self._save()
            return policy

    def get_policy(self, tenant_id: str | None = None) -> ResidencyPolicy | None:
        """获取驻留策略。"""
        if not is_enabled("compliance"):
            return None
        tid = tenant_id or get_current_tenant_id()
        with self._lock:
            self._load()
            return self._policies.get(tid)

    # ==================================================================
    # 跨境检查
    # ==================================================================

    def check_location(self, data: Any, current_region: str) -> DataRegion:
        """检查数据当前所在区域是否合规。

        Args:
            data: 任意数据(用于检查 metadata.region)
            current_region: 当前实际所在区域
        """
        if not is_enabled("compliance"):
            return DataRegion.GLOBAL

        region = DataRegion(current_region)
        tid = get_current_tenant_id()
        policy = self._policies.get(tid) if tid else None
        if policy is None:
            # 无策略 = GLOBAL(允许)
            return DataRegion.GLOBAL

        if region != policy.primary_region and region not in policy.allowed_regions:
            raise ResidencyViolation(
                f"Data in {region.value} but tenant {tid} requires {policy.primary_region.value}",
                src_region=policy.primary_region.value,
                dst_region=region.value,
            )
        return region

    def ensure_in_region(
        self,
        data: Any,
        target_region: str,
        data_kind: str = "",
    ) -> None:
        """确保数据写入目标区域是允许的。

        Raises:
            ResidencyViolation: 跨境违规
        """
        if not is_enabled("compliance"):
            return

        target = DataRegion(target_region)
        tid = get_current_tenant_id()
        policy = self._policies.get(tid) if tid else None
        if policy is None:
            return  # 无策略 = 不限制

        # 敏感数据有专属 region
        if data_kind and data_kind in policy.sensitive_data_regions:
            required = policy.sensitive_data_regions[data_kind]
            if target != required:
                raise ResidencyViolation(
                    f"Sensitive data '{data_kind}' must be in {required.value}, got {target.value}",
                    src_region=required.value,
                    dst_region=target.value,
                    data_kind=data_kind,
                )
            return

        # 普通数据:必须在 primary 或 allowed_regions
        if target != policy.primary_region and target not in policy.allowed_regions:
            if not policy.cross_border_consent:
                raise ResidencyViolation(
                    f"Cross-border to {target.value} requires consent (tenant={tid})",
                    src_region=policy.primary_region.value,
                    dst_region=target.value,
                    data_kind=data_kind,
                )

    def export_controlled(
        self,
        user_id: str,
        target_region: str,
        data_kind: str = "",
    ) -> ExportResult:
        """受控跨境导出(已获同意 + 审计)。

        Returns:
            ExportResult: 导出结果(若违规则 success=False)
        """
        if not is_enabled("compliance"):
            return ExportResult(
                True, user_id, target_region, data_kind, False, "compliance_disabled"
            )

        tid = get_current_tenant_id()
        policy = self._policies.get(tid) if tid else None
        if policy is None:
            return ExportResult(True, user_id, target_region, data_kind, False, "no_policy")

        target = DataRegion(target_region)
        if target == policy.primary_region or target in policy.allowed_regions:
            return ExportResult(True, user_id, target_region, data_kind, True, "within_allowed")

        if policy.cross_border_consent:
            return ExportResult(True, user_id, target_region, data_kind, True, "consented")

        return ExportResult(
            False,
            user_id,
            target_region,
            data_kind,
            False,
            "no_consent",
        )

    # ==================================================================
    # 内部
    # ==================================================================

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = yaml.safe_load(self.store_path.read_text(encoding="utf-8")) or {}
                for tid, pdata in data.get("policies", {}).items():
                    self._policies[tid] = ResidencyPolicy(
                        tenant_id=pdata["tenant_id"],
                        primary_region=DataRegion(pdata["primary_region"]),
                        allowed_regions=[DataRegion(r) for r in pdata.get("allowed_regions", [])],
                        cross_border_consent=pdata.get("cross_border_consent", False),
                        cross_border_consent_at=pdata.get("cross_border_consent_at"),
                        sensitive_data_regions={
                            k: DataRegion(v)
                            for k, v in pdata.get("sensitive_data_regions", {}).items()
                        },
                    )
        except Exception as e:
            logger.warning("Residency store load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "policies": {
                    tid: {
                        "tenant_id": p.tenant_id,
                        "primary_region": p.primary_region.value,
                        "allowed_regions": [r.value for r in p.allowed_regions],
                        "cross_border_consent": p.cross_border_consent,
                        "cross_border_consent_at": p.cross_border_consent_at,
                        "sensitive_data_regions": {
                            k: v.value for k, v in p.sensitive_data_regions.items()
                        },
                    }
                    for tid, p in self._policies.items()
                },
            }
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(
                yaml.safe_dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8"
            )
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Residency store save failed: %s", e)


# 全局单例
_dr_instance: DataResidency | None = None
_dr_lock = threading.Lock()


def get_data_residency() -> DataResidency:
    global _dr_instance
    if _dr_instance is None:
        with _dr_lock:
            if _dr_instance is None:
                _dr_instance = DataResidency()
    return _dr_instance
