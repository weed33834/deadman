"""哀伤陪伴模块测试

覆盖：危机等级识别（L0/L1/正常）、L0 安全协议护栏、LLM 路径降级。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deadman.grief import (
    CRISIS_HOTLINE_TEXT,
    companion_reply,
    detect_crisis,
)


def test_detect_normal():
    c = detect_crisis("我今天想把存款整理一下")
    assert c.level == 0
    assert not c.triggered_hotline


def test_detect_l1_grief():
    c = detect_crisis("我好想他，心里空荡荡的，睡不着")
    assert c.level == 1
    assert not c.triggered_hotline


def test_detect_l0_self_harm_triggers_hotline():
    c = detect_crisis("我觉得活着没意思，不想活了")
    assert c.level == 2
    assert c.triggered_hotline is True


def test_l0_response_includes_verified_hotline():
    out = __import__("asyncio").run(companion_reply("活着没意思，我不想活了", llm=None))
    assert out["crisis"]["level"] == 2
    assert out["used_llm"] is False
    # 不编造号码：只含已验证热线
    assert "400-161-9995" in out["reply"]
    assert "120" in out["reply"] or "110" in out["reply"]
    assert CRISIS_HOTLINE_TEXT in out["reply"]


def test_rule_fallback_without_llm_l1():
    out = __import__("asyncio").run(companion_reply("好难过，走不出来", llm=None))
    assert out["crisis"]["level"] == 1
    assert "陪" in out["reply"]


class _FakeLLM:
    def __init__(self, text: str = "我在，我听到你了。"):
        self.text = text

    async def chat(self, messages, max_tokens=600):
        return self.text


def test_llm_path_returns_reply():
    out = __import__("asyncio").run(
        companion_reply("我有点不知所措", llm=_FakeLLM("我会陪着你梳理。"), history=[])
    )
    assert out["used_llm"] is True
    assert out["reply"] == "我会陪着你梳理。"


def test_llm_path_l0_bypasses_llm():
    # L0 时即使传入 llm 也只走安全协议，不展开话题
    out = __import__("asyncio").run(
        companion_reply("我不想活了", llm=_FakeLLM("should-not-appear"))
    )
    assert out["crisis"]["level"] == 2
    assert out["used_llm"] is False
    assert "should-not-appear" not in out["reply"]


def test_llm_failure_falls_back():
    class _Boom:
        async def chat(self, messages, max_tokens=600):
            raise RuntimeError("network down")

    out = __import__("asyncio").run(companion_reply("我有点乱", llm=_Boom()))
    assert out["used_llm"] is False
    assert "陪" in out["reply"]
