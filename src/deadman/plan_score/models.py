"""plan_score 数据模型

定义五个评分维度 Enum + 单维度 SubScore + 总分 PlanScore。

设计要点：
    - 评分维度固定为 5 个，便于横向对比与历史回溯
    - SubScore.score 范围 [0, 100]，便于加权汇总
    - completed_items / missing_items 用 list[str] 描述具体条目，
      让前端能渲染清单（参考 Trust & Will 的缺失项 UI）
    - suggestions 限单维度内建议；overall_suggestions 是 top-3 跨维度
      优先建议（由 PlanScorer._generate_suggestions 汇总）

合规关联：
    - integrity-framework.md L1：completed_items / missing_items
      必须反映实际加载到的数据，不编造
    - service-boundary-framework.md L3：suggestions 不得含"建议这样做
      即可达到法律效力"等越界表述
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Category(str, Enum):
    """评分维度枚举

    str 子类便于 JSON 序列化（与 SwitchState 设计一致）。
    """

    ENDING_NOTE = "ENDING_NOTE"  # 终活笔记（9 章节完整度）
    VAULT = "VAULT"  # 数字遗产保险库（条目类型与配置）
    DECEDENT_CASE = "DECEDENT_CASE"  # 遗码通案例（创建/事件/归档）
    DEADMAN_SWITCH = "DEADMAN_SWITCH"  # 失联开关（联系人/律师/继承人）
    BASIC_INFO = "BASIC_INFO"  # 用户基础信息（邮箱/昵称/留存）


@dataclass
class SubScore:
    """单维度评分

    Attributes:
        category: 维度枚举
        score: 0-100 整数
        completed_items: 已完成的具体条目（如 "personal_info 已填写"）
        missing_items: 缺失的具体条目（如 "未填写 medical_wishes 章节"）
        suggestions: 该维度内的具体改进建议
    """

    category: Category
    score: int
    completed_items: list[str] = field(default_factory=list)
    missing_items: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "score": self.score,
            "completed_items": list(self.completed_items),
            "missing_items": list(self.missing_items),
            "suggestions": list(self.suggestions),
        }


@dataclass
class PlanScore:
    """完整度评分总结果

    Attributes:
        user_id: 用户 ID
        total_score: 加权后总分 0-100
        category_scores: 5 个维度的 SubScore 列表
        overall_suggestions: 跨维度 top-3 优先建议
        generated_at: 评分生成时间戳
    """

    user_id: str
    total_score: int
    category_scores: list[SubScore] = field(default_factory=list)
    overall_suggestions: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "total_score": self.total_score,
            "category_scores": [s.to_dict() for s in self.category_scores],
            "overall_suggestions": list(self.overall_suggestions),
            "generated_at": self.generated_at.isoformat()
            if isinstance(self.generated_at, datetime)
            else self.generated_at,
        }
