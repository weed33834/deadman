"""P8.17 AI 治理框架 - 模型卡 (Google Model Card Toolkit 风格)。

借鉴 Google Model Card Toolkit (Mitchell et al., 2019) 和
Hugging Face Model Card 规范,为每个 agent / 模型 / 子模型建立元数据卡,
记录 intended use / limitations / ethical considerations / 训练数据摘要 / 评估指标。

模块结构:
    - ModelCard: 单个模型的元数据卡 (dataclass)
    - ModelCardRegistry: 卡片注册中心 (持久化到 JSON)

设计:
    - 每个模型 / agent 注册一张卡,便于审计 / 透明度报告 / 责任归属
    - 卡片支持版本化 (version 字段) + 归档 (deprecated)
    - 卡片字段与 Google Model Card Schema 对齐 (subset)
    - 持久化到 data/governance/model_cards.json (按租户隔离)
    - 原子写 (.tmp + os.replace) + 线程安全 (RLock)

feature flag:`DEADMAN_GOVERNANCE_ENABLED=0` 关闭时操作静默 no-op (返回 None)。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path

logger = logging.getLogger(__name__)


@dataclass
class ModelCard:
    """单个模型的元数据卡 (Google Model Card 风格)。

    Attributes:
        model_id: 模型 / agent 唯一 ID (如 "deadman-memorial-writer-v1")
        name: 人类可读名称
        version: 语义版本 (如 "1.0.0")
        description: 详细描述
        owner: 负责人 / 团队
        date_created: 创建时间戳 (epoch seconds)
        intended_use: 预期用途 (list of strings)
        not_for_use: 不适用场景 (list of strings)
        capabilities: 能力清单
        limitations: 限制清单
        ethical_considerations: 伦理考量 (list of strings)
        training_data_summary: 训练数据摘要 (来源 / 规模 / 时间)
        evaluation_metrics: 评估指标 (dict of metric → value)
        fairness_metrics: 公平性指标 (dict of group → metric → value)
        contact: 联系方式 (邮箱 / 团队主页)
        archived: 是否已归档 (deprecated)
    """

    model_id: str
    name: str
    version: str = "0.1.0"
    description: str = ""
    owner: str = ""
    date_created: float = field(default_factory=time.time)
    intended_use: list[str] = field(default_factory=list)
    not_for_use: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    ethical_considerations: list[str] = field(default_factory=list)
    training_data_summary: str = ""
    evaluation_metrics: dict[str, float] = field(default_factory=dict)
    fairness_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    contact: str = ""
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelCard":
        return cls(
            model_id=data["model_id"],
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            owner=data.get("owner", ""),
            date_created=float(data.get("date_created", time.time())),
            intended_use=list(data.get("intended_use", [])),
            not_for_use=list(data.get("not_for_use", [])),
            capabilities=list(data.get("capabilities", [])),
            limitations=list(data.get("limitations", [])),
            ethical_considerations=list(data.get("ethical_considerations", [])),
            training_data_summary=data.get("training_data_summary", ""),
            evaluation_metrics=dict(data.get("evaluation_metrics", {})),
            fairness_metrics=dict(data.get("fairness_metrics", {})),
            contact=data.get("contact", ""),
            archived=bool(data.get("archived", False)),
        )


class ModelCardRegistry:
    """模型卡注册中心 - 管理 model_id → ModelCard 映射。

    持久化到 ``data/governance/model_cards.json`` (按租户隔离)。
    线程安全 + 原子写。
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store_path = store_path or resolve_data_path("governance/model_cards.json")
        self._lock = threading.RLock()
        self._cache: dict[str, ModelCard] = {}
        self._loaded = False

    def register(self, card: ModelCard) -> ModelCard:
        """注册 / 更新一张模型卡。"""
        if not is_enabled("governance"):
            logger.debug("Governance disabled, skip model card register")
            return card
        with self._lock:
            self._load()
            self._cache[card.model_id] = card
            self._save()
            logger.info("Model card registered: %s (%s)", card.model_id, card.version)
            return card

    def get(self, model_id: str) -> Optional[ModelCard]:
        """按 ID 获取模型卡。"""
        with self._lock:
            self._load()
            return self._cache.get(model_id)

    def list_all(self) -> list[ModelCard]:
        """列出所有模型卡 (含已归档)。"""
        with self._lock:
            self._load()
            return list(self._cache.values())

    def list_active(self) -> list[ModelCard]:
        """仅列出未归档的模型卡。"""
        with self._lock:
            self._load()
            return [c for c in self._cache.values() if not c.archived]

    def update(self, model_id: str, **fields: Any) -> Optional[ModelCard]:
        """更新模型卡字段 (部分字段)。"""
        with self._lock:
            self._load()
            card = self._cache.get(model_id)
            if card is None:
                return None
            for k, v in fields.items():
                if hasattr(card, k):
                    setattr(card, k, v)
            self._save()
            return card

    def archive(self, model_id: str) -> Optional[ModelCard]:
        """归档模型 (deprecated,不再用于新决策)。"""
        with self._lock:
            self._load()
            card = self._cache.get(model_id)
            if card is None:
                return None
            card.archived = True
            self._save()
            logger.info("Model card archived: %s", model_id)
            return card

    def delete(self, model_id: str) -> bool:
        """硬删除模型卡 (一般用 archive 代替)。"""
        with self._lock:
            self._load()
            if model_id in self._cache:
                del self._cache[model_id]
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
                    self._cache[cid] = ModelCard.from_dict(cdata)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Load model cards failed: %s", e)
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
            logger.error("Save model cards failed: %s", e)


# 全局单例
_mcr_instance: Optional[ModelCardRegistry] = None
_mcr_lock = threading.Lock()


def get_model_card_registry() -> ModelCardRegistry:
    global _mcr_instance
    if _mcr_instance is None:
        with _mcr_lock:
            if _mcr_instance is None:
                _mcr_instance = ModelCardRegistry()
    return _mcr_instance
