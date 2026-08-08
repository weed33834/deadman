"""思维意识识别 - 意图 + 状态联合感知与路由

把「用户意图」(awareness/intent) 与「安全状态」(grief.detect_crisis) 合并，
产出统一的 AwarenessResult，并给出推荐能力（对应工具 / 智能体）。

这是能力栈「思维意识识别」层的对外入口：上层编排在分发动作前先调用 assess()，
据此选择路由，从而实现「理解用户当下想要什么、处于什么状态」。

安全优先：若检测到 L0 危机，无论意图如何，推荐能力一律为 grief_support，
并标记 needs_crisis_intervention=True。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .intent import IntentType, classify_intent

# 意图 → 推荐能力（对应工具 / 智能体 / 页面）
_CAPABILITY_MAP: dict[IntentType, str] = {
    IntentType.WILL: "ending_note",           # 终活笔记 / 遗嘱引导
    IntentType.FUNERAL: "knowledge_procedure", # 办事流程向导
    IntentType.GRIEF: "grief_companion",       # 哀伤陪伴
    IntentType.DIGITAL_LEGACY: "digital_legacy",  # 数字遗产清单工具
    IntentType.DEAD_SWITCH: "deadman_switch",  # 死人开关
    IntentType.MEMORIAL: "memorial_writer",    # 纪念文生成
    IntentType.KNOWLEDGE: "knowledge_query",   # 地域知识库查询
    IntentType.GENERAL: "chat",                # 通用对话
}


@dataclass
class AwarenessResult:
    intent: str
    intent_confidence: float
    crisis_level: int
    recommended_capability: str
    needs_crisis_intervention: bool = False
    intent_scores: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "intent_confidence": round(self.intent_confidence, 3),
            "crisis_level": self.crisis_level,
            "recommended_capability": self.recommended_capability,
            "needs_crisis_intervention": self.needs_crisis_intervention,
            "intent_scores": self.intent_scores,
        }


async def assess(text: str, llm=None) -> AwarenessResult:
    """联合评估意图与安全状态，返回路由建议。"""
    from ..grief import detect_crisis

    intent_res = await classify_intent(text, llm=llm)
    crisis = detect_crisis(text)

    # 安全优先：L0 危机一律走哀伤陪伴 + 危机干预
    if crisis.level == 2:
        return AwarenessResult(
            intent=intent_res.intent.value,
            intent_confidence=intent_res.confidence,
            crisis_level=crisis.level,
            recommended_capability="grief_companion",
            needs_crisis_intervention=True,
            intent_scores=intent_res.scores,
        )

    return AwarenessResult(
        intent=intent_res.intent.value,
        intent_confidence=intent_res.confidence,
        crisis_level=crisis.level,
        recommended_capability=_CAPABILITY_MAP.get(
            intent_res.intent, "chat"
        ),
        needs_crisis_intervention=False,
        intent_scores=intent_res.scores,
    )
