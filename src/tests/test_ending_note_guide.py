"""测试 deadman.ending_note.guide - AI 引导填写

覆盖点（7 个）：
    - test_next_question_first_section              首次引导返回第一章
    - test_next_question_skips_filled               跳过已填章节
    - test_next_question_all_filled_returns_done    全填完返回完成
    - test_save_answer_masks_pii                    保存时自动脱敏
    - test_safety_signal_detection_high             检测"不想活"返回 high 风险
    - test_safety_signal_detection_none             普通文本无风险
    - test_completion_rate_calculation              完整度计算

测试隔离：每个测试用 tmp_path fixture 独立数据目录，互不污染。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadman.ending_note.guide import EndingNoteGuide
from deadman.ending_note.models import EndingNote
from deadman.ending_note.store import EndingNoteStore


@pytest.fixture
def guide(tmp_path: Path) -> EndingNoteGuide:
    """每个测试独立的 guide + store"""
    store = EndingNoteStore(data_dir=tmp_path)
    return EndingNoteGuide(store=store)


# ====================================================================
# 1. next_question 引导主流程
# ====================================================================


class TestNextQuestion:
    def test_next_question_first_section(self, guide: EndingNoteGuide):
        """首次引导返回第一章 personal_info"""
        note = EndingNote.new("user-1")
        section, title, question = guide.next_question(note)
        assert section == "personal_info"
        assert "第一章" in title
        assert "姓名" in question or "基本情况" in question

    def test_next_question_skips_filled(self, guide: EndingNoteGuide):
        """跳过已填章节，返回下一未填章节"""
        note = EndingNote.new("user-2")
        # 填写第 1、2 章
        note.personal_info = {"full_name_masked": "张**"}
        note.family_relations = [{"relation": "配偶", "name_masked": "李**"}]
        section, title, question = guide.next_question(note)
        # 第 3 章 assets
        assert section == "assets"
        assert "第三章" in title
        assert "资产" in question

    def test_next_question_all_filled_returns_done(self, guide: EndingNoteGuide):
        """全填完返回 __done__"""
        note = EndingNote.new("user-3")
        note.personal_info = {"full_name_masked": "张**"}
        note.family_relations = [{"relation": "配偶", "name_masked": "李**"}]
        note.assets = [{"type": "房产", "description_masked": "北京市**"}]
        note.funeral_wishes = {"type": "火葬"}
        note.medical_wishes = {"life_sustaining": False}
        note.digital_legacy = [{"platform": "微信", "account_masked": "138****1234"}]
        note.messages = [{"recipient": "配偶", "content": "感谢"}]
        note.emergency_contacts = [{"role": "律师", "name_masked": "王**"}]
        note.will_intent = {"has_formal_will": False}

        section, title, question = guide.next_question(note)
        assert section == "__done__"
        assert "已完成" in title or "完成" in question

    def test_next_question_safety_signal_blocks(self, guide: EndingNoteGuide):
        """检测到自杀风险信号后停止流程引导，返回 __safety__"""
        note = EndingNote.new("user-4")
        # 模拟之前已检测到自杀风险
        note.safety_flags = {
            "contains_suicidal_ideation": True,
            "last_reviewed_at": "2026-07-21T10:00:00",
            "needs_professional_review": True,
        }
        section, title, question = guide.next_question(note)
        assert section == "__safety__"
        assert "安全" in title
        assert "心理危机" in question or "急救" in question


# ====================================================================
# 2. save_answer 自动脱敏 + 安全信号检测
# ====================================================================


class TestSaveAnswer:
    def test_save_answer_masks_pii(self, guide: EndingNoteGuide):
        """保存时自动脱敏"""
        note = EndingNote.new("user-5")
        note = guide.save_answer(
            note,
            "personal_info",
            {
                "full_name": "李四",
                "birth_date": "1970-01-01",
                "occupation": "教师",
            },
        )
        # 应脱敏
        assert note.personal_info is not None
        assert note.personal_info.get("full_name_masked") == "李**"
        assert note.personal_info.get("birth_date_masked") == "1970"
        # 非 PII 字段保留
        assert note.personal_info.get("occupation") == "教师"
        # 不应残留原始 PII 字段
        assert "full_name" not in note.personal_info
        assert "birth_date" not in note.personal_info

    def test_save_answer_unknown_section_raises(self, guide: EndingNoteGuide):
        note = EndingNote.new("user-6")
        with pytest.raises(ValueError, match="未知章节"):
            guide.save_answer(note, "nonexistent_section", {"foo": "bar"})

    def test_save_answer_touches_updated_at(self, guide: EndingNoteGuide):
        """save_answer 应更新 updated_at"""
        note = EndingNote.new("user-7")
        old_updated = note.updated_at
        # 强制时间差
        import time

        time.sleep(0.01)
        note = guide.save_answer(
            note,
            "personal_info",
            {"full_name": "张三"},
        )
        assert note.updated_at > old_updated


# ====================================================================
# 3. 安全信号检测
# ====================================================================


class TestSafetySignals:
    def test_safety_signal_detection_high(self, guide: EndingNoteGuide):
        """检测"不想活"返回 high 风险

        safety-protocol.md 第一章识别信号：表达自伤/自杀意图
        """
        result = guide._check_safety_signals("我最近不想活了，太累了")
        assert result["contains_signal"] is True
        assert result["severity"] == "high"
        assert "停止流程引导" in result["suggested_action"]

    def test_safety_signal_detection_none(self, guide: EndingNoteGuide):
        """普通文本无风险"""
        result = guide._check_safety_signals("我想把房产留给我儿子")
        assert result["contains_signal"] is False
        assert result["severity"] == "none"

    def test_safety_signal_detection_high_other_keywords(self, guide: EndingNoteGuide):
        """其他 high 关键词也能识别"""
        for kw in ["自杀", "想死", "了结", "一了百了", "不想拖累"]:
            result = guide._check_safety_signals(f"我考虑过{kw}")
            assert result["contains_signal"] is True, f"关键词 {kw} 未被识别"
            assert result["severity"] == "high"

    def test_safety_signal_detection_medium(self, guide: EndingNoteGuide):
        """中度风险关键词"""
        result = guide._check_safety_signals("我开始安排好后事")
        assert result["contains_signal"] is True
        assert result["severity"] == "medium"

    def test_save_answer_sets_safety_flag_on_signal(self, guide: EndingNoteGuide):
        """检测到风险信号时设置 safety_flags.contains_suicidal_ideation=True"""
        note = EndingNote.new("user-8")
        note = guide.save_answer(
            note,
            "messages",
            {"recipient": "家人", "content": "我最近不想活了，太累了"},
        )
        flags = note.safety_flags or {}
        assert flags.get("contains_suicidal_ideation") is True
        assert flags.get("needs_professional_review") is True
        assert flags.get("last_reviewed_at") is not None


# ====================================================================
# 4. 完整度计算
# ====================================================================


class TestCompletionRate:
    def test_completion_rate_empty(self, guide: EndingNoteGuide):
        """空笔记完整度 0"""
        note = EndingNote.new("user-9")
        rate = guide.completion_rate(note)
        assert rate["overall"] == 0.0
        for v in rate["sections"].values():
            assert v == 0.0

    def test_completion_rate_partial(self, guide: EndingNoteGuide):
        """部分填写"""
        note = EndingNote.new("user-10")
        note.personal_info = {"full_name_masked": "张**"}
        note.family_relations = [{"relation": "配偶"}]
        rate = guide.completion_rate(note)
        # 2/9 ≈ 0.222
        assert 0.20 < rate["overall"] < 0.25
        assert rate["sections"]["personal_info"] == 1.0
        assert rate["sections"]["family_relations"] == 1.0
        assert rate["sections"]["assets"] == 0.0

    def test_completion_rate_full(self, guide: EndingNoteGuide):
        """全部填写 100%"""
        note = EndingNote.new("user-11")
        note.personal_info = {"full_name_masked": "张**"}
        note.family_relations = [{"relation": "配偶"}]
        note.assets = [{"type": "房产"}]
        note.funeral_wishes = {"type": "火葬"}
        note.medical_wishes = {"life_sustaining": False}
        note.digital_legacy = [{"platform": "微信"}]
        note.messages = [{"recipient": "配偶"}]
        note.emergency_contacts = [{"role": "律师"}]
        note.will_intent = {"has_formal_will": False}
        rate = guide.completion_rate(note)
        assert rate["overall"] == 1.0
        for v in rate["sections"].values():
            assert v == 1.0

    def test_completion_rate_empty_dict_counts_as_unfilled(self, guide: EndingNoteGuide):
        """空 dict / 空 list 应计为未填写"""
        note = EndingNote.new("user-12")
        note.personal_info = {}  # 空 dict
        note.family_relations = []  # 空 list
        rate = guide.completion_rate(note)
        assert rate["sections"]["personal_info"] == 0.0
        assert rate["sections"]["family_relations"] == 0.0
