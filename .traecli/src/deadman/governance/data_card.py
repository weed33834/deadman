"""P8.17 AI 治理框架 - 数据卡 (Datasheets for Datasets 风格)。

借鉴 Gebru et al., 2018 "Datasheets for Datasets" 规范,
为每个数据集建立元数据卡,记录 source / collection method / processing / PII /
consent / sensitivity,便于数据治理 / 合规审计 / 跨团队数据共享。

模块结构:
    - DataCard: 单个数据集的元数据卡 (dataclass)
    - DataCardRegistry: 卡片注册中心 (持久化到 JSON)

设计:
    - sensitivity_level 四级:public / internal / confidential / restricted
    - pii_categories 标准 PII 类别 (借鉴 GDPR / PIPL)
    - consent_required 标识是否需用户同意 (受限数据集默认 True)
    - retention_period 数据保留期限 (秒 / 天数,通过 cron 强制)
    - 持久化到 data/governance/data_cards.json (按租户隔离)
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


class SensitivityLevel(str, Enum):
    """数据敏感度分级 (借鉴《数据安全法》分级 + GDPR 分类)。

    PUBLIC:       公开数据 (可对外发布)
    INTERNAL:     内部数据 (仅团队内部可见)
    CONFIDENTIAL: 机密数据 (需授权访问,如用户身份信息)
    RESTRICTED:   受限数据 (受法规约束,如医疗 / 金融 / 未成年人)
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    def requires_consent(self) -> bool:
        """高敏感度数据默认需要用户同意。"""
        return self in (SensitivityLevel.CONFIDENTIAL, SensitivityLevel.RESTRICTED)

    def sensitivity_rank(self) -> int:
        """敏感度排序 (越大越敏感)。"""
        order = {
            SensitivityLevel.PUBLIC: 0,
            SensitivityLevel.INTERNAL: 1,
            SensitivityLevel.CONFIDENTIAL: 2,
            SensitivityLevel.RESTRICTED: 3,
        }
        return order[self]


@dataclass
class DataCard:
    """单个数据集的元数据卡 (Datasheets for Datasets 风格)。

    Attributes:
        dataset_id: 数据集唯一 ID
        name: 人类可读名称
        version: 语义版本
        description: 详细描述 (内容 / 用途 / 范围)
        owner: 负责人 / 团队
        source: 数据来源 (URL / 系统名 / 用户上传)
        collection_method: 收集方式 (主动上传 / API 抓取 / 第三方购买)
        processing_steps: 处理步骤 (清洗 / 脱敏 / 标注)
        license: 数据许可 (CC-BY / 商业 / 内部)
        retention_period: 保留期限 (秒,0 = 永久)
        pii_categories: PII 类别 (name / phone / id_card / address / email / medical / financial)
        consent_required: 是否需要用户同意
        sensitivity_level: 敏感度分级 (枚举)
        archived: 是否已归档
    """

    dataset_id: str
    name: str
    version: str = "0.1.0"
    description: str = ""
    owner: str = ""
    source: str = ""
    collection_method: str = ""
    processing_steps: list[str] = field(default_factory=list)
    license: str = ""
    retention_period: int = 0
    pii_categories: list[str] = field(default_factory=list)
    consent_required: bool = False
    sensitivity_level: SensitivityLevel = SensitivityLevel.INTERNAL
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sensitivity_level"] = self.sensitivity_level.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataCard":
        sens = data.get("sensitivity_level", "internal")
        try:
            sens_enum = SensitivityLevel(sens)
        except ValueError:
            sens_enum = SensitivityLevel.INTERNAL
        return cls(
            dataset_id=data["dataset_id"],
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            owner=data.get("owner", ""),
            source=data.get("source", ""),
            collection_method=data.get("collection_method", ""),
            processing_steps=list(data.get("processing_steps", [])),
            license=data.get("license", ""),
            retention_period=int(data.get("retention_period", 0)),
            pii_categories=list(data.get("pii_categories", [])),
            consent_required=bool(data.get("consent_required", False)),
            sensitivity_level=sens_enum,
            archived=bool(data.get("archived", False)),
        )


class DataCardRegistry:
    """数据卡注册中心 - 管理 dataset_id → DataCard 映射。

    持久化到 ``data/governance/data_cards.json`` (按租户隔离)。
    线程安全 + 原子写。
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or resolve_data_path("governance/data_cards.json")
        self._lock = threading.RLock()
        self._cache: dict[str, DataCard] = {}
        self._loaded = False

    def register(self, card: DataCard) -> DataCard:
        """注册 / 更新一张数据卡。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip data card register")
            return card
        with self._lock:
            self._load()
            self._cache[card.dataset_id] = card
            self._save()
            logger.info(
                "Data card registered: %s (sensitivity=%s)",
                card.dataset_id,
                card.sensitivity_level.value,
            )
            return card

    def get(self, dataset_id: str) -> Optional[DataCard]:
        """按 ID 获取数据卡。"""
        with self._lock:
            self._load()
            return self._cache.get(dataset_id)

    def list_all(self) -> list[DataCard]:
        """列出所有数据卡。"""
        with self._lock:
            self._load()
            return list(self._cache.values())

    def list_active(self) -> list[DataCard]:
        """仅列出未归档的数据卡。"""
        with self._lock:
            self._load()
            return [c for c in self._cache.values() if not c.archived]

    def list_by_sensitivity(self, min_level: SensitivityLevel) -> list[DataCard]:
        """列出敏感度 >= min_level 的数据卡。"""
        with self._lock:
            self._load()
            min_rank = min_level.sensitivity_rank()
            return [
                c for c in self._cache.values()
                if c.sensitivity_level.sensitivity_rank() >= min_rank
            ]

    def update(self, dataset_id: str, **fields: Any) -> Optional[DataCard]:
        """更新数据卡字段。"""
        with self._lock:
            self._load()
            card = self._cache.get(dataset_id)
            if card is None:
                return None
            # 处理 sensitivity_level 字符串 → 枚举
            if "sensitivity_level" in fields and isinstance(fields["sensitivity_level"], str):
                try:
                    fields["sensitivity_level"] = SensitivityLevel(fields["sensitivity_level"])
                except ValueError:
                    fields.pop("sensitivity_level")
            for k, v in fields.items():
                if hasattr(card, k):
                    setattr(card, k, v)
            self._save()
            return card

    def archive(self, dataset_id: str) -> Optional[DataCard]:
        """归档数据集。"""
        with self._lock:
            self._load()
            card = self._cache.get(dataset_id)
            if card is None:
                return None
            card.archived = True
            self._save()
            logger.info("Data card archived: %s", dataset_id)
            return card

    def delete(self, dataset_id: str) -> bool:
        """硬删除数据卡。"""
        with self._lock:
            self._load()
            if dataset_id in self._cache:
                del self._cache[dataset_id]
                self._save()
                return True
            return False

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
                    self._cache[cid] = DataCard.from_dict(cdata)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load data cards failed: %s", e)
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
            logger.error("Save data cards failed: %s", e)


# 全局单例
_dcr_instance: Optional[DataCardRegistry] = None
_dcr_lock = threading.Lock()


def get_data_card_registry() -> DataCardRegistry:
    global _dcr_instance
    if _dcr_instance is None:
        with _dcr_lock:
            if _dcr_instance is None:
                _dcr_instance = DataCardRegistry()
    return _dcr_instance
