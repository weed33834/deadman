"""测试 P6.4 Drift 检测 - DriftDetector

覆盖点：
  - 数值漂移检测（简化 KS 检验）能检测到分布偏移
  - 数值漂移检测在分布相同时不报告漂移
  - 分布漂移检测（PSI）能检测到分布偏移
  - 文本漂移检测能检测到词汇分布偏移
  - feature flag 关闭时返回空报告
"""

from __future__ import annotations

import pytest

from deadman.observability.drift import (
    DRIFT_DETECTION_ENABLED,
    DriftDetector,
    DriftReport,
)


@pytest.fixture
def enabled_drift(monkeypatch):
    """临时启用 DRIFT_DETECTION_ENABLED"""
    monkeypatch.setattr(
        "deadman.observability.drift.DRIFT_DETECTION_ENABLED", True
    )
    yield


# =====================================================================
# 数值漂移检测
# =====================================================================


class TestNumericDrift:
    """detect_numeric_drift 行为测试"""

    def test_numeric_drift_detects_shift(self, enabled_drift):
        """分布显著偏移时应报告漂移"""
        detector = DriftDetector(threshold=0.3)
        # baseline 集中在 100-200，current 集中在 300-400 → 完全分离
        report = detector.detect_numeric_drift(
            "latency",
            baseline_samples=[100, 120, 150, 180, 200],
            current_samples=[300, 320, 350, 380, 400],
        )

        assert report.metric_name == "latency"
        assert report.method == "ks"
        # 完全分离的分布 KS D = 1.0
        assert report.drift_score == pytest.approx(1.0)
        assert report.drifted is True
        assert report.baseline_value == pytest.approx(150.0)  # 均值
        assert report.current_value == pytest.approx(350.0)

    def test_numeric_drift_no_shift(self, enabled_drift):
        """分布相同时不应报告漂移"""
        detector = DriftDetector(threshold=0.3)
        # 两组样本来自同一分布
        report = detector.detect_numeric_drift(
            "latency",
            baseline_samples=[100, 120, 150, 180, 200],
            current_samples=[105, 125, 155, 175, 195],
        )

        # 两组重叠很多，KS D 应小于阈值
        assert report.drift_score < 0.3
        assert report.drifted is False
        assert report.method == "ks"

    def test_numeric_drift_partial_shift(self, enabled_drift):
        """部分偏移时 KS D 应在 (0, 1) 之间"""
        detector = DriftDetector(threshold=0.5)
        # 3 个 baseline 100/120/150 + 3 个 current 150/180/200
        report = detector.detect_numeric_drift(
            "latency",
            baseline_samples=[100, 120, 150],
            current_samples=[150, 180, 200],
        )
        # 至少有部分重叠（150）
        assert 0.0 < report.drift_score <= 1.0

    def test_numeric_drift_empty_samples(self, enabled_drift):
        """样本为空时返回 drifted=False"""
        detector = DriftDetector()
        report = detector.detect_numeric_drift("latency", [], [1, 2, 3])
        assert report.drifted is False
        assert report.details["reason"] == "samples_empty"

        report2 = detector.detect_numeric_drift("latency", [1, 2], [1])
        assert report2.drifted is False
        assert report2.details["reason"] == "samples_too_few"


# =====================================================================
# 分布漂移检测（PSI）
# =====================================================================


class TestDistributionDrift:
    """detect_distribution_drift 行为测试"""

    def test_distribution_drift_psi(self, enabled_drift):
        """分布显著偏移时 PSI 应较大且 drifted=True"""
        detector = DriftDetector(threshold=0.1)
        # baseline 完全在 bin A，current 完全在 bin B → 完全偏移
        report = detector.detect_distribution_drift(
            "category_dist",
            baseline_dist={"A": 100, "B": 0},
            current_dist={"A": 0, "B": 100},
        )

        assert report.method == "psi"
        # 完全偏移时 PSI 应较大
        assert report.drift_score > 0.1
        assert report.drifted is True

    def test_distribution_drift_no_shift(self, enabled_drift):
        """分布相同时 PSI 接近 0 且 drifted=False"""
        detector = DriftDetector(threshold=0.1)
        report = detector.detect_distribution_drift(
            "category_dist",
            baseline_dist={"A": 50, "B": 50},
            current_dist={"A": 50, "B": 50},
        )
        # 完全相同的分布 PSI = 0
        assert report.drift_score == pytest.approx(0.0, abs=1e-6)
        assert report.drifted is False

    def test_distribution_drift_empty(self, enabled_drift):
        """分布为空时返回 drifted=False"""
        detector = DriftDetector()
        report = detector.detect_distribution_drift(
            "category_dist", {}, {"A": 1}
        )
        assert report.drifted is False
        assert report.details["reason"] == "dist_empty"


# =====================================================================
# 文本漂移检测
# =====================================================================


class TestTextDrift:
    """detect_text_drift 行为测试"""

    def test_text_drift_vocabulary_shift(self, enabled_drift):
        """词汇分布显著偏移时应报告漂移"""
        detector = DriftDetector(threshold=0.2)
        # baseline 用中文 A 主题，current 用完全不同的主题
        report = detector.detect_text_drift(
            "user_query",
            baseline_texts=[
                "户籍注销流程是什么",
                "如何办理户籍注销",
                "户籍注销需要什么材料",
            ],
            current_texts=[
                "今天天气真好啊",
                "天气真不错",
                "明天会不会下雨",
            ],
        )

        assert report.method == "vocabulary"
        # 完全不同的词汇 → drift_score 应较大
        assert report.drift_score > 0.2
        assert report.drifted is True
        # current_only_words 应非空
        assert len(report.details["current_only_words"]) > 0

    def test_text_drift_no_shift(self, enabled_drift):
        """词汇分布相似时不应报告漂移"""
        detector = DriftDetector(threshold=0.5)
        # 两组文本完全相同 → PSI=0, current_only_ratio=0
        report = detector.detect_text_drift(
            "user_query",
            baseline_texts=["how to cancel account", "cancel account steps"],
            current_texts=["how to cancel account", "cancel account steps"],
        )
        # 完全相同的文本，drift_score 应为 0
        assert report.drift_score == pytest.approx(0.0, abs=1e-6)
        assert report.drifted is False
        # 无 current 独有词
        assert report.details["current_only_ratio"] == 0.0

    def test_text_drift_empty(self, enabled_drift):
        """文本为空时返回 drifted=False"""
        detector = DriftDetector()
        report = detector.detect_text_drift(
            "user_query", [], ["hello world"]
        )
        assert report.drifted is False
        assert report.details["reason"] == "texts_empty"


# =====================================================================
# feature flag 关闭
# =====================================================================


class TestDriftDisabled:
    """feature flag 关闭行为测试"""

    def test_drift_disabled_noop(self):
        """feature flag 关闭时所有 detect_* 返回 drifted=False 空报告"""
        from deadman.observability import drift as drift_module
        original = drift_module.DRIFT_DETECTION_ENABLED
        drift_module.DRIFT_DETECTION_ENABLED = False
        try:
            detector = DriftDetector(threshold=0.2)

            # 数值漂移
            r1 = detector.detect_numeric_drift(
                "latency",
                baseline_samples=[100, 200, 300],
                current_samples=[1000, 2000, 3000],
            )
            assert r1.drifted is False
            assert r1.method == "disabled"
            assert r1.details["reason"] == "drift_detection_disabled"

            # 分布漂移
            r2 = detector.detect_distribution_drift(
                "dist", {"A": 100}, {"B": 100}
            )
            assert r2.drifted is False
            assert r2.method == "disabled"

            # 文本漂移
            r3 = detector.detect_text_drift(
                "text", ["a b c"], ["x y z"]
            )
            assert r3.drifted is False
            assert r3.method == "disabled"
        finally:
            drift_module.DRIFT_DETECTION_ENABLED = original
