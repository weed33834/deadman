"""测试 deadman.selfcheck - SelfCheckGPT 数字类幻觉检测

覆盖点：
  - extract_numeric_claims 6 种正则模式（phone/days/money/percent/article/step_count）
  - check_consistency 一致性计算（用 mock 采样响应）
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from deadman.selfcheck.checker import (
    NUMBER_PATTERNS,
    SelfCheckChecker,
    _HIGH_THRESHOLD,
)


# =====================================================================
# extract_numeric_claims - 6 种正则模式
# =====================================================================


class TestExtractNumericClaims:
    """测试 extract_numeric_claims 6 种数字类 claim 提取"""

    async def test_patterns_count(self):
        # 6 种正则模式
        assert len(NUMBER_PATTERNS) == 6

    async def test_pattern_types(self):
        # 6 种类型齐全
        expected = {"phone", "days", "money", "percent", "article", "step_count"}
        assert set(NUMBER_PATTERNS.keys()) == expected

    async def test_extract_phone(self):
        # 电话号码
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("请拨打 010-12345678 联系")
        phone_claims = [c for c in claims if c["type"] == "phone"]
        assert len(phone_claims) >= 1
        assert "010-12345678" in phone_claims[0]["claim"] or "12345678" in phone_claims[0]["claim"]

    async def test_extract_phone_11_digit(self):
        # 11 位手机号
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("手机号 13800138000")
        phone_claims = [c for c in claims if c["type"] == "phone"]
        assert len(phone_claims) >= 1

    async def test_extract_days(self):
        # 时限（天/工作日/日）
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("办理需要 30天 完成")
        days_claims = [c for c in claims if c["type"] == "days"]
        assert len(days_claims) >= 1
        assert "30" in days_claims[0]["claim"]

    async def test_extract_days_workday(self):
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("大约 15个工作日")
        days_claims = [c for c in claims if c["type"] == "days"]
        assert len(days_claims) >= 1

    async def test_extract_money(self):
        # 金额（元/万/美元/人民币）
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("费用约 5000元")
        money_claims = [c for c in claims if c["type"] == "money"]
        assert len(money_claims) >= 1
        assert "5000" in money_claims[0]["claim"]

    async def test_extract_money_wan(self):
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("资产 100万")
        money_claims = [c for c in claims if c["type"] == "money"]
        assert len(money_claims) >= 1

    async def test_extract_percent(self):
        # 百分比
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("通过率 85.5%")
        percent_claims = [c for c in claims if c["type"] == "percent"]
        assert len(percent_claims) >= 1
        assert "85.5" in percent_claims[0]["claim"]

    async def test_extract_article(self):
        # 法条号
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("根据民法典第 1145 条")
        article_claims = [c for c in claims if c["type"] == "article"]
        assert len(article_claims) >= 1
        assert "1145" in article_claims[0]["claim"]

    async def test_extract_step_count(self):
        # 步骤数
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("流程共 5 步")
        step_claims = [c for c in claims if c["type"] == "step_count"]
        assert len(step_claims) >= 1
        assert "5" in step_claims[0]["claim"]

    async def test_extract_multiple_types(self):
        # 同一文本多种类型都能提取
        checker = SelfCheckChecker()
        text = "费用 5000元，办理 30天，第 1145 条规定，通过率 85%"
        claims = await checker.extract_numeric_claims(text)
        types_found = {c["type"] for c in claims}
        assert "money" in types_found
        assert "days" in types_found
        assert "article" in types_found
        assert "percent" in types_found

    async def test_extract_no_claims(self):
        # 无数字类 claim → 空列表
        checker = SelfCheckChecker()
        claims = await checker.extract_numeric_claims("请咨询当地医保部门。")
        assert claims == []

    async def test_claims_sorted_by_position(self):
        # 按位置排序
        checker = SelfCheckChecker()
        text = "第 100 条 与 第 1 条"
        claims = await checker.extract_numeric_claims(text)
        if len(claims) >= 2:
            assert claims[0]["position"] <= claims[1]["position"]


# =====================================================================
# check_consistency - 一致性计算
# =====================================================================


class TestCheckConsistency:
    """测试 check_consistency 一致性计算"""

    async def test_all_samples_match_high_consistency(self):
        # 原始 claim 在所有采样中都出现 → 一致性 1.0
        # 注意：days 正则为 \b\d+\s*(?:天|个工作日|日)\b，"天" 后跟中文会破坏 \b 边界
        # 故样本中 "30天" 后应是非汉字或字符串结尾
        checker = SelfCheckChecker()
        original = "办理需要 30天"
        # 3 次采样都包含 30天（且 30天 后为空白或字符串结尾以满足 \b）
        samples = ["办理需要 30天", "约 30天 完成", "30天 处理"]
        result = await checker.check_consistency(original, samples)

        assert "claims" in result
        assert "overall_consistency" in result
        # 30天 在所有采样中都出现 → 一致性 1.0
        assert result["overall_consistency"] == 1.0
        # 标签为"高"
        days_claim = [c for c in result["claims"] if c["type"] == "days"][0]
        assert days_claim["label"] == "高"
        assert days_claim["consistency"] == 1.0

    async def test_no_samples_match_low_consistency(self):
        # 原始 claim 在采样中都不出现 → 一致性 0.0
        checker = SelfCheckChecker()
        original = "办理需要 30天"
        # 采样中都不含 30天
        samples = ["办理需要 15天", "约 7天", "45天左右"]
        result = await checker.check_consistency(original, samples)

        days_claim = [c for c in result["claims"] if c["type"] == "days"][0]
        assert days_claim["consistency"] == 0.0
        assert days_claim["label"] == "未知"

    async def test_partial_match_medium_consistency(self):
        # 部分采样匹配 → 一致性介于 0 和 1 之间
        checker = SelfCheckChecker()
        original = "办理需要 30天"
        # 3 次采样中 1 次匹配 → 一致性 1/3
        samples = ["办理需要 30天", "约 15天", "45天"]
        result = await checker.check_consistency(original, samples)
        days_claim = [c for c in result["claims"] if c["type"] == "days"][0]
        assert days_claim["consistency"] == pytest.approx(1.0 / 3.0)

    async def test_empty_samples_returns_zero(self):
        # 无采样 → overall_consistency=0.0
        checker = SelfCheckChecker()
        result = await checker.check_consistency("30天", [])
        assert result["overall_consistency"] == 0.0
        assert result["claims"] == []

    async def test_no_claims_in_original(self):
        # 原始响应无数字类 claim → claims 为空
        checker = SelfCheckChecker()
        result = await checker.check_consistency("无数字", ["sample1", "sample2"])
        assert result["claims"] == []
        assert result["overall_consistency"] == 0.0

    async def test_high_threshold_value(self):
        # 高一致性阈值 0.8
        assert _HIGH_THRESHOLD == 0.8

    async def test_normalize_removes_whitespace(self):
        # 归一化应去除空白
        assert SelfCheckChecker._normalize("30 天") == "30天"
        assert SelfCheckChecker._normalize("第 1145 条") == "第1145条"

    async def test_label_high(self):
        # 一致性 >= 0.8 → 高
        checker = SelfCheckChecker()
        assert checker._label_for_consistency(0.8) == "高"
        assert checker._label_for_consistency(1.0) == "高"

    async def test_label_medium(self):
        # 一致性 >= 阈值（默认 0.5）且 < 0.8 → 中
        checker = SelfCheckChecker()
        assert checker._label_for_consistency(0.6) == "中"
        assert checker._label_for_consistency(0.5) == "中"

    async def test_label_unknown(self):
        # 一致性 < 阈值 → 未知
        checker = SelfCheckChecker()
        assert checker._label_for_consistency(0.3) == "未知"
        assert checker._label_for_consistency(0.0) == "未知"


# =====================================================================
# check - 主入口
# =====================================================================


class TestSelfCheckMain:
    """测试 check 主入口"""

    async def test_no_numeric_claims_passes(self):
        # 无数字类 claim → 直接通过
        checker = SelfCheckChecker()
        mock_llm = AsyncMock()
        result = await checker.check("无数字的响应", [{"role": "user", "content": "x"}], mock_llm)
        assert result["passed"] is True
        assert result["reason"] == "no_numeric_claims"

    async def test_check_with_consistent_samples(self):
        # 一致的采样 → 通过
        checker = SelfCheckChecker()
        mock_llm = AsyncMock()
        # mock 采样返回包含 30天 的响应
        mock_llm.sample_multiple = AsyncMock(return_value=[
            "办理需要 30天", "办理需要 30天", "办理需要 30天"
        ])
        result = await checker.check(
            "办理需要 30天",
            [{"role": "user", "content": "问题"}],
            mock_llm,
        )
        assert result["passed"] is True
        assert result["numeric_claims_found"] >= 1

    async def test_check_with_inconsistent_samples(self):
        # 不一致的采样 → 不通过
        checker = SelfCheckChecker()
        mock_llm = AsyncMock()
        # mock 采样返回不含 30天 的响应
        mock_llm.sample_multiple = AsyncMock(return_value=[
            "办理需要 15天", "办理需要 7天", "办理需要 45天"
        ])
        result = await checker.check(
            "办理需要 30天",
            [{"role": "user", "content": "问题"}],
            mock_llm,
        )
        assert result["passed"] is False
        assert len(result["low_consistency_claims"]) >= 1
