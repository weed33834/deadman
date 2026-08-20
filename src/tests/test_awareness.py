"""思维意识识别层测试

覆盖：意图关键词分类、LLM 兜底、危机优先路由、能力映射。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deadman.awareness import (
    IntentType,
    assess,
    classify_intent_keyword,
)


def test_intent_will():
    r = classify_intent_keyword("我想立一份遗嘱，分配我的财产")
    assert r.intent == IntentType.WILL
    assert r.confidence > 0


def test_intent_funeral():
    r = classify_intent_keyword("家人去世了，怎么开死亡证明和销户？")
    assert r.intent == IntentType.FUNERAL


def test_intent_digital_legacy():
    r = classify_intent_keyword("我的微信账号和游戏账号要怎么当做数字遗产处理")
    assert r.intent == IntentType.DIGITAL_LEGACY


def test_intent_dead_switch():
    r = classify_intent_keyword("我想设个死人开关，万一我不在了自动发送遗言")
    assert r.intent == IntentType.DEAD_SWITCH


def test_intent_memorial():
    r = classify_intent_keyword("帮我写一篇悼文和墓志铭")
    assert r.intent == IntentType.MEMORIAL


def test_intent_knowledge():
    r = classify_intent_keyword("民法典里关于继承的规定是怎样的")
    assert r.intent == IntentType.KNOWLEDGE


def test_intent_general_fallback():
    r = classify_intent_keyword("今天天气不错")
    assert r.intent == IntentType.GENERAL
    assert r.confidence == 0.0


def test_assess_routes_funeral():
    out = __import__("asyncio").run(assess("怎么办理丧葬费和公积金提取"))
    assert out.intent == IntentType.FUNERAL.value
    assert out.recommended_capability == "knowledge_procedure"
    assert out.needs_crisis_intervention is False


def test_assess_crisis_overrides_intent():
    # 即使文本含其他意图词，L0 危机一律走 grief_companion + 干预
    out = __import__("asyncio").run(assess("我不想活了，想跟着走了"))
    assert out.crisis_level == 2
    assert out.needs_crisis_intervention is True
    assert out.recommended_capability == "grief_companion"


def test_assess_grief_no_crisis():
    out = __import__("asyncio").run(assess("我母亲走了，我好想她，心里空空的"))
    assert out.intent == IntentType.GRIEF.value
    assert out.crisis_level == 1
    assert out.recommended_capability == "grief_companion"
    assert out.needs_crisis_intervention is False


class _FakeLLM:
    def __init__(self, label: str = "general"):
        self.label = label

    async def chat(self, messages, max_tokens=20):
        return self.label


def test_assess_llm_override_on_low_confidence():
    # 关键词无命中时，LLM 给出 digital_legacy 标签
    out = __import__("asyncio").run(
        assess("我离开以后这些线上的事情该怎么处理", llm=_FakeLLM("digital_legacy"))
    )
    assert out.intent == IntentType.DIGITAL_LEGACY.value
    assert out.intent_confidence >= 0.7
