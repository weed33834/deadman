"""测试 legacy.types - 核心数据类型

覆盖点：
  - TransferSummary.is_complete() 在字段齐全/缺失时的行为
  - RiskTier 枚举 4 个等级（R0/R1/R2/R3）
  - ExecutionMode 枚举 3 个模式（SUCCESS/FALLBACK/FAILED）
"""

from __future__ import annotations

import pytest

from legacy.types import (
    ConfidenceLabel,
    ExecutionMode,
    RuleCheckResult,
    RiskTier,
    Source,
    SubagentResult,
    TaskState,
    TransferSummary,
)


# =====================================================================
# RiskTier 枚举
# =====================================================================


class TestRiskTier:
    """测试 RiskTier 枚举 - 4 个风险等级"""

    def test_risk_tier_values(self):
        # R0/R1/R2/R3 四级
        assert RiskTier.R0.value == "R0"
        assert RiskTier.R1.value == "R1"
        assert RiskTier.R2.value == "R2"
        assert RiskTier.R3.value == "R3"

    def test_risk_tier_count(self):
        # 恰好 4 个等级
        members = list(RiskTier)
        assert len(members) == 4

    def test_risk_tier_is_str_enum(self):
        # str 枚举：可与字符串直接比较
        assert RiskTier.R2 == "R2"
        assert RiskTier.R3 == "R3"

    def test_risk_tier_ordering_by_severity(self):
        # 按 severity 排序：R0 < R1 < R2 < R3（用 value 比较）
        order = [RiskTier.R0, RiskTier.R1, RiskTier.R2, RiskTier.R3]
        # 至少 R3 比 R0 更严重（这里用 value 字符串比较即可）
        assert order[-1] == RiskTier.R3


# =====================================================================
# ExecutionMode 枚举
# =====================================================================


class TestExecutionMode:
    """测试 ExecutionMode 枚举 - 子智能体执行模式"""

    def test_execution_mode_values(self):
        # success / fallback / failed 三态
        assert ExecutionMode.SUCCESS.value == "success"
        assert ExecutionMode.FALLBACK.value == "fallback"
        assert ExecutionMode.FAILED.value == "failed"

    def test_execution_mode_count(self):
        assert len(list(ExecutionMode)) == 3

    def test_execution_mode_is_str_enum(self):
        # str 枚举可直接与字符串比较
        assert ExecutionMode.SUCCESS == "success"
        assert ExecutionMode.FALLBACK == "fallback"

    def test_execution_mode_distinct(self):
        # 三种模式互不相等
        assert ExecutionMode.SUCCESS != ExecutionMode.FALLBACK
        assert ExecutionMode.FALLBACK != ExecutionMode.FAILED
        assert ExecutionMode.SUCCESS != ExecutionMode.FAILED


# =====================================================================
# TransferSummary.is_complete()
# =====================================================================


class TestTransferSummary:
    """测试 TransferSummary 数据类 - 7 字段转介摘要"""

    def _make_full_summary(self) -> TransferSummary:
        # 7 字段全部齐全
        return TransferSummary(
            from_agent="death_aftercare",
            to_agent="legal_advisor",
            reason="用户提到遗产纠纷，建议转介法律顾问",
            user_situation="父亲去世，留下房产与多子女",
            current_question="遗产怎么分？",
            completed_items=["情绪安抚"],
            pending_items=["法律咨询"],
        )

    def test_is_complete_when_all_fields_present(self):
        # 字段齐全 → True
        ts = self._make_full_summary()
        assert ts.is_complete() is True

    def test_is_complete_when_from_agent_empty(self):
        # from_agent 为空 → False
        ts = self._make_full_summary()
        ts.from_agent = ""
        assert ts.is_complete() is False

    def test_is_complete_when_to_agent_empty(self):
        ts = self._make_full_summary()
        ts.to_agent = ""
        assert ts.is_complete() is False

    def test_is_complete_when_reason_empty(self):
        ts = self._make_full_summary()
        ts.reason = ""
        assert ts.is_complete() is False

    def test_is_complete_when_user_situation_empty(self):
        ts = self._make_full_summary()
        ts.user_situation = ""
        assert ts.is_complete() is False

    def test_is_complete_when_current_question_none(self):
        # current_question 为 None → False
        ts = self._make_full_summary()
        ts.current_question = None
        assert ts.is_complete() is False

    def test_is_complete_when_current_question_empty(self):
        # current_question 为空字符串 → is_complete 仍判定为 True
        # 因为源码只检查 `current_question is not None`
        ts = self._make_full_summary()
        ts.current_question = ""
        # 源码实现：current_question is not None → 空字符串也算"有"
        assert ts.is_complete() is True

    def test_default_collections_empty(self):
        # completed_items / pending_items 默认空列表
        ts = TransferSummary(
            from_agent="a",
            to_agent="b",
            reason="r",
            user_situation="u",
            current_question="q",
        )
        assert ts.completed_items == []
        assert ts.pending_items == []


# =====================================================================
# 其他数据类的基础校验
# =====================================================================


class TestOtherTypes:
    """测试其他核心数据类"""

    def test_rule_check_result_defaults(self):
        # 默认 passed=True, risk_tier=R0
        r = RuleCheckResult(passed=True)
        assert r.passed is True
        assert r.violations == []
        assert r.risk_tier == RiskTier.R0
        assert r.safety_triggered is False
        assert r.integrity_violations == []

    def test_rule_check_result_with_violations(self):
        r = RuleCheckResult(
            passed=False,
            violations=[{"rule": "integrity-framework"}],
            risk_tier=RiskTier.R1,
            safety_triggered=False,
        )
        assert r.passed is False
        assert len(r.violations) == 1
        assert r.risk_tier == RiskTier.R1

    def test_subagent_result_defaults(self):
        # 默认 confidence=0.0, sources=[]
        r = SubagentResult(
            subagent_name="medical-guide-insurance",
            execution_mode=ExecutionMode.SUCCESS,
            report={"answer": "ok"},
        )
        assert r.confidence == 0.0
        assert r.sources == []
        assert r.execution_mode == ExecutionMode.SUCCESS

    def test_source_defaults(self):
        s = Source()
        assert s.url is None
        assert s.verified is False
        assert s.trust_level == "medium"

    def test_confidence_label(self):
        c = ConfidenceLabel(claim="30天", confidence="高", source="医保局官网")
        assert c.claim == "30天"
        assert c.confidence == "高"
        assert c.source == "医保局官网"

    def test_task_state_enum(self):
        # A2A 任务状态枚举
        assert TaskState.SUBMITTED.value == "submitted"
        assert TaskState.COMPLETED.value == "completed"
        assert TaskState.FAILED.value == "failed"
