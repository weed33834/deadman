"""memorial_writer 数据模型

MemorialRequest  - 用户请求（doc_type + 逝者信息 + 风格偏好）
MemorialResult   - 生成结果（text + confidence + safety_flags + alternatives）

合规关联：
    - PIPL 第五章：decedent_name 仅用于本次生成，不落盘
    - integrity-framework.md：personality_traits / memories / values_or_sayings
      必须由用户提供；AI 不编造未提供的特质
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# =====================================================================
# doc_type 常量
# =====================================================================
DOC_TYPE_EULOGY = "eulogy"
DOC_TYPE_OBITUARY = "obituary"
DOC_TYPE_THANK_YOU_NOTE = "thank_you_note"
DOC_TYPE_EPITAPH = "epitaph"
DOC_TYPE_MEMORIAL_SPEECH = "memorial_speech"

# 5 种 doc_type 的元信息（用于 CLI/Web 列表 + 字数约束）
DOC_TYPES: dict[str, dict[str, Any]] = {
    DOC_TYPE_EULOGY: {
        "name": "悼文",
        "name_en": "Eulogy",
        "description": "在追悼会上诵读的悼念文章，回顾逝者一生、表达哀思",
        "word_range": (500, 800),
    },
    DOC_TYPE_OBITUARY: {
        "name": "讣告",
        "name_en": "Obituary",
        "description": "对外公布的逝者信息公告，含生卒日期、丧礼时间地点",
        "word_range": (200, 400),
    },
    DOC_TYPE_THANK_YOU_NOTE: {
        "name": "答谢词",
        "name_en": "Thank-You Note",
        "description": "家属对吊唁者的感谢致辞，常在追悼会结尾宣读",
        "word_range": (200, 400),
    },
    DOC_TYPE_EPITAPH: {
        "name": "墓志铭",
        "name_en": "Epitaph",
        "description": "镌刻在墓碑上的简短铭文，凝练逝者一生",
        "word_range": (20, 100),
    },
    DOC_TYPE_MEMORIAL_SPEECH: {
        "name": "追思会致辞",
        "name_en": "Memorial Speech",
        "description": "追思会上的长篇致辞，含回忆、感悟、告别",
        "word_range": (500, 1000),
    },
}

# 语气
VALID_TONES = ("solemn", "warm", "humorous")
# 信仰
VALID_FAITHS = ("none", "buddhist", "taoist", "christian")
# 语言
VALID_LANGUAGES = ("zh-CN", "en-US", "zh-Classical")


@dataclass
class MemorialRequest:
    """悼文生成请求

    Attributes:
        doc_type: 文档类型（eulogy/obituary/thank_you_note/epitaph/memorial_speech）
        decedent_name: 逝者姓名或称呼（如"先父""张老先生"，可化名）
        relationship: 与逝者的关系（如"儿子""配偶""孙女"）
        personality_traits: 逝者性格特质列表（用户提供的，AI 不编造）
        memories: 共同回忆列表（用户提供的具体事件/场景）
        values_or_sayings: 价值观或口头禅列表（用户提供的）
        tone: 语气（solemn 庄重 / warm 温暖 / humorous 幽默但得体）
        faith: 信仰背景（none / buddhist / taoist / christian）
        language: 语言（zh-CN / en-US / zh-Classical）
        word_limit: 字数上限（0 表示用 doc_type 默认 word_range 上限）
    """

    doc_type: str
    decedent_name: str
    relationship: str = "家属"
    personality_traits: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    values_or_sayings: list[str] = field(default_factory=list)
    tone: str = "solemn"
    faith: str = "none"
    language: str = "zh-CN"
    word_limit: int = 0

    def validate(self) -> list[str]:
        """校验请求字段，返回错误消息列表（空列表 = 通过）

        不抛异常，便于调用方聚合多字段错误返回给用户。
        """
        errors: list[str] = []
        if self.doc_type not in DOC_TYPES:
            errors.append(
                f"doc_type 必须是 {list(DOC_TYPES.keys())} 之一，当前: {self.doc_type}"
            )
        if not self.decedent_name or not self.decedent_name.strip():
            errors.append("decedent_name 不能为空")
        if self.tone not in VALID_TONES:
            errors.append(
                f"tone 必须是 {list(VALID_TONES)} 之一，当前: {self.tone}"
            )
        if self.faith not in VALID_FAITHS:
            errors.append(
                f"faith 必须是 {list(VALID_FAITHS)} 之一，当前: {self.faith}"
            )
        if self.language not in VALID_LANGUAGES:
            errors.append(
                f"language 必须是 {list(VALID_LANGUAGES)} 之一，当前: {self.language}"
            )
        if self.word_limit < 0:
            errors.append("word_limit 不能为负数")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "decedent_name": self.decedent_name,
            "relationship": self.relationship,
            "personality_traits": list(self.personality_traits),
            "memories": list(self.memories),
            "values_or_sayings": list(self.values_or_sayings),
            "tone": self.tone,
            "faith": self.faith,
            "language": self.language,
            "word_limit": self.word_limit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MemorialRequest:
        """从 dict 构造（用于 Web 端点解析 body）"""
        return cls(
            doc_type=d.get("doc_type", ""),
            decedent_name=d.get("decedent_name", ""),
            relationship=d.get("relationship", "家属"),
            personality_traits=list(d.get("personality_traits", []) or []),
            memories=list(d.get("memories", []) or []),
            values_or_sayings=list(d.get("values_or_sayings", []) or []),
            tone=d.get("tone", "solemn"),
            faith=d.get("faith", "none"),
            language=d.get("language", "zh-CN"),
            word_limit=int(d.get("word_limit", 0) or 0),
        )


@dataclass
class MemorialResult:
    """悼文生成结果

    Attributes:
        text: 主稿全文
        doc_type: 文档类型（与请求一致）
        confidence: 生成可信度（0-1）。LLM 可用时 0.7-0.9，降级时 0.3
        safety_flags: 安全标记 dict（含 self_harm/violence/inappropriate 布尔字段）
        alternatives: 备选草稿列表（同结构 dict，0-2 个备选）
    """

    text: str
    doc_type: str
    confidence: float = 0.0
    safety_flags: dict[str, bool] = field(default_factory=dict)
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "doc_type": self.doc_type,
            "confidence": self.confidence,
            "safety_flags": dict(self.safety_flags),
            "alternatives": list(self.alternatives),
        }
