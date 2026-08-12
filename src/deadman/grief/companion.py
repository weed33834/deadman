"""哀伤陪伴模块 - 面向丧亲者的共情陪伴与危机护栏

对标 Akeeva / SafeKeep 等竞品的「bereavement support」能力：
    - 共情、非评判的陪伴式对话（不诊断、不开导、不比较）
    - 危机识别（自伤 / 轻生意向）触发 L0 安全协议，优先人身安全
    - 仅引用已验证的官方热线（不编造号码）

遵守：
    - rules/safety-protocol.md 第一章（心理危机识别与应对）
    - rules/legal-compliance-framework.md（PIPL 最小必要、去标识化）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------
# 危机信号（关键词驱动，零依赖、可测试）
# ---------------------------------------------------------------------
# L0：自伤 / 轻生意图 —— 立即触发安全协议
_L0_SIGNALS = (
    "不想活",
    "活不下去",
    "撑不下去",
    "想死",
    "去死",
    "结束这一切",
    "跟着走",
    "活着没意思",
    "没有意义",
    "解脱",
    "轻生",
    "自伤",
    "自杀",
    "了断",
)
# L1：强烈哀伤 / 绝望（非立即危险，但需温柔承接）
_L1_SIGNALS = (
    "好难过",
    "走不出来",
    "崩溃",
    "失眠",
    "睡不着",
    "吃不下",
    "喘不过气",
    "空荡荡",
    "空空的",
    "心里空",
    "好想他",
    "好想她",
    "思念",
    "痛苦",
    "孤独",
    "麻木",
    "为什么是他",
    "为什么会这样",
)

# 已验证官方热线（不编造号码；参见 render 审计 ALLOWED_NUMBERS）
CRISIS_HOTLINE_TEXT = (
    "如果你正经历难以承受的情绪，请优先联系专业帮助：\n"
    "  · 全国 24 小时心理援助热线：400-161-9995\n"
    "  · 全国卫生热线：12320\n"
    "  · 紧急情况请拨打当地急救电话 120 或报警电话 110。\n"
    "你不用独自扛着，有人愿意陪你。"
)

# 系统提示：哀伤陪伴风格约束
COMPANION_SYSTEM_PROMPT = (
    "你是身后事平台的哀伤陪伴助手，陪伴正在经历丧失的人。\n"
    "原则（严格遵守）：\n"
    "1. 共情、接纳、非评判；不诊断（如『你这是抑郁症』）、不开导（如『想开点』）、不比较（如『有人比你更惨』）。\n"
    "2. 不替用户做决定，不承诺用户不会自伤，不假装自己是心理咨询师。\n"
    "3. 当用户表达自伤或轻生意向时，立即停止事务性内容，优先人身安全，"
    "并转介已提供的官方心理援助热线，温和询问『你现在身边有人吗』。\n"
    "4. 回应简洁温暖，多用『我听到你了』『这很不容易』式的承接，避免长篇说教。\n"
    "5. 涉及具体丧葬 / 法律事务时，提示可切换到相应向导，不在此展开。"
)


@dataclass
class CrisisAssessment:
    level: int = 0  # 0 正常 / 1 哀伤困扰 / 2 L0 危机
    matched: list[str] = field(default_factory=list)
    triggered_hotline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "matched": self.matched,
            "triggered_hotline": self.triggered_hotline,
        }


def detect_crisis(text: str) -> CrisisAssessment:
    """关键词识别危机等级。无外部依赖，确定性可测试。"""
    t = (text or "").lower()
    l0 = [s for s in _L0_SIGNALS if s in t]
    if l0:
        return CrisisAssessment(level=2, matched=l0, triggered_hotline=True)
    l1 = [s for s in _L1_SIGNALS if s in t]
    if l1:
        return CrisisAssessment(level=1, matched=l1)
    return CrisisAssessment(level=0)


def _l0_response() -> str:
    return (
        "我听到你了，此刻的痛一定很沉重。我无法替你保密你可能会伤害自己的事，"
        "因为你的安全比什么都重要。\n"
        f"{CRISIS_HOTLINE_TEXT}\n"
        "你现在身边有人陪着吗？如果可以，先联系一个你信任的人，或拨打上面的电话。"
    )


async def companion_reply(
    text: str,
    llm=None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """生成哀伤陪伴回复。

    Returns:
        {
            "reply": str,
            "crisis": dict,         # CrisisAssessment.to_dict()
            "used_llm": bool,      # 是否走 LLM（false=规则回退）
        }
    """
    crisis = detect_crisis(text)

    # L0：直接走安全协议，不调用 LLM 展开话题
    if crisis.level == 2:
        return {"reply": _l0_response(), "crisis": crisis.to_dict(), "used_llm": False}

    # 规则兜底（无 LLM 时）
    if llm is None:
        return {
            "reply": (
                "我听到你了，失去重要的人真的很难。你愿意多说一点吗？我会陪着你。"
                if crisis.level == 1
                else "我在这里陪着你。无论你想说什么，都可以慢慢说。"
            ),
            "crisis": crisis.to_dict(),
            "used_llm": False,
        }

    # LLM 增强：用陪伴系统提示生成共情回复
    try:
        messages: list[dict[str, str]] = [{"role": "system", "content": COMPANION_SYSTEM_PROMPT}]
        for h in history or []:
            messages.append(h)
        messages.append({"role": "user", "content": text})
        reply = await llm.chat(messages, max_tokens=600)
        return {"reply": reply, "crisis": crisis.to_dict(), "used_llm": True}
    except Exception:
        # LLM 失败不阻断陪伴主体流程，回退规则
        return {
            "reply": "我听到你了，失去重要的人真的很难。我会陪着你。",
            "crisis": crisis.to_dict(),
            "used_llm": False,
        }
