"""测试 deadman.disclaimer - 免责告知模块

覆盖点（3 个）：
  - test_full_opening_contains_required_disclaimers: 完整告知含 4 类（identity/legal/agent/data）
  - test_short_reminder_legal: legal 场景返回法律免责
  - test_for_web_footer_is_concise: Web footer 长度 < 200 字

依据：
- compliance-framework.md（四项禁止）
- legal-compliance-framework.md（告知时机）
- transparency-framework.md（AI 身份告知 + 能力边界 + 不确定性告知）
"""

from __future__ import annotations

import pytest

from deadman.disclaimer.text import DisclaimerBuilder

# =====================================================================
# 1. 完整开场告知含 4 类
# =====================================================================


class TestFullOpening:
    """测试 DisclaimerBuilder.full_opening()"""

    def test_full_opening_contains_required_disclaimers(self) -> None:
        text = DisclaimerBuilder.full_opening()

        # 4 类告知都必须出现
        assert "deadman" in text, "应含平台身份（deadman）"
        assert "信息引导工具" in text, "应说明是信息引导工具"
        assert "不销售" in text, "应声明不销售殡葬产品"
        assert "不分殡仪馆分成" in text or "不与殡仪馆分成" in text, "应声明不与殡仪馆分成"

        # 法律意见免责
        assert "不提供法律意见" in text, "应含法律意见免责"
        assert "律师" in text, "应引导咨询律师"

        # 代办边界免责
        assert "不代办" in text, "应含代办边界免责"
        assert "死亡证明" in text, "应说明死亡证明办理路径"

        # 数据准确性免责
        assert "公开资料整理" in text, "应含数据准确性免责"
        assert "核实" in text, "应提示核实"

    def test_full_opening_has_four_paragraphs(self) -> None:
        # 4 段告知用 \n\n 分隔
        text = DisclaimerBuilder.full_opening()
        paragraphs = text.split("\n\n")
        assert len(paragraphs) == 4, "完整告知应含 4 段"


# =====================================================================
# 2. 场景化简短提醒
# =====================================================================


class TestShortReminder:
    """测试 DisclaimerBuilder.short_reminder(scenario)"""

    def test_short_reminder_legal(self) -> None:
        text = DisclaimerBuilder.short_reminder("legal")
        assert "不提供法律意见" in text
        assert "律师" in text or "公证处" in text

    def test_short_reminder_agent(self) -> None:
        text = DisclaimerBuilder.short_reminder("agent")
        assert "不代办" in text

    def test_short_reminder_data(self) -> None:
        text = DisclaimerBuilder.short_reminder("data")
        assert "公开资料整理" in text
        assert "核实" in text

    def test_short_reminder_identity(self) -> None:
        text = DisclaimerBuilder.short_reminder("identity")
        assert "deadman" in text
        assert "信息引导工具" in text

    def test_short_reminder_unknown_scenario_raises(self) -> None:
        with pytest.raises(ValueError):
            DisclaimerBuilder.short_reminder("unknown")


# =====================================================================
# 3. Web 页面底部固定告知
# =====================================================================


class TestWebFooter:
    """测试 DisclaimerBuilder.for_web_footer()"""

    def test_for_web_footer_is_concise(self) -> None:
        text = DisclaimerBuilder.for_web_footer()
        # 长度 < 200 字
        assert len(text) < 200, f"Web footer 应 < 200 字，当前 {len(text)} 字"

        # 必须覆盖核心边界
        assert "信息引导工具" in text, "应说明是信息引导工具"
        assert "不代办" in text, "应声明不代办"
        assert "不出法律意见" in text or "不替代" in text, "应声明不出法律意见/不替代"
        assert "核实" in text, "应提示核实"
