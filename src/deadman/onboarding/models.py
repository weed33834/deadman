"""Onboarding 画像数据模型 - Phase 16C

OnboardingProfile 字段语义：
- user_id：所属用户
- relationship：与逝者的关系（亲属/朋友/本人/其他）
- location：用户所在省份（用于地域知识库加载）
- death_date：逝者去世日期（ISO，可空——用户为本人才不填）
- current_stage：当前已办理到的阶段（多选 stage 名）
- consent_disclaimer：是否已读免责声明
- created_at / updated_at：时间戳
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OnboardingProfile:
    """Onboarding 收集的用户画像"""

    user_id: str
    relationship: str  # 亲属 / 朋友 / 本人 / 其他
    location: str  # 省份
    death_date: str | None  # ISO 日期 YYYY-MM-DD；本人无
    current_stage: list[str] = field(default_factory=list)
    consent_disclaimer: bool = False
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _utcnow_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    def touch(self) -> None:
        """更新 updated_at"""
        self.updated_at = _utcnow_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "relationship": self.relationship,
            "location": self.location,
            "death_date": self.death_date,
            "current_stage": list(self.current_stage),
            "consent_disclaimer": bool(self.consent_disclaimer),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OnboardingProfile:
        return cls(
            user_id=data["user_id"],
            relationship=data["relationship"],
            location=data["location"],
            death_date=data.get("death_date"),
            current_stage=list(data.get("current_stage", [])),
            consent_disclaimer=bool(data.get("consent_disclaimer", False)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
