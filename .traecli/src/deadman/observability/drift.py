"""P6.4 Drift 检测 - 检测指标/分布/文本漂移

参考 DEADMAN_UPGRADE_PLAN.md v1.2 P6 实施细化。

支持 3 类漂移检测：
  1. detect_numeric_drift - 数值漂移（自实现简化 KS 检验）
  2. detect_distribution_drift - 分布漂移（PSI: Population Stability Index）
  3. detect_text_drift - 文本漂移（词汇分布偏移）

Feature flag: DEADMAN_DRIFT_DETECTION_ENABLED=0 默认关闭
  - 关闭时所有 detect_* 返回 drifted=False 的空报告
  - 开启时执行检测

降级路径全覆盖：
  1. feature flag 关闭 → 返回 drifted=False 空报告
  2. 样本为空 → 返回 drifted=False 空报告
  3. 样本数量过少 → 返回 drifted=False 空报告
  4. 不强制引入 scipy/numpy（纯 stdlib 实现）

注意：自实现 KS 检验是简化版，仅用于内部告警，不作统计严谨性保证。
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
DRIFT_DETECTION_ENABLED: bool = os.environ.get(
    "DEADMAN_DRIFT_DETECTION_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class DriftReport:
    """漂移检测报告

    Attributes:
        metric_name: 检测的指标/分布名
        baseline_value: 基线值（baseline 中心度量，如均值）
        current_value: 当前值（current 中心度量，如均值）
        drift_score: 漂移分数（KS 统计量 / PSI / 词汇偏移率）
        drifted: 是否漂移（drift_score > threshold）
        threshold: 触发告警的阈值
    """

    metric_name: str = ""
    baseline_value: float = 0.0
    current_value: float = 0.0
    drift_score: float = 0.0
    drifted: bool = False
    threshold: float = 0.2
    method: str = ""  # "ks" / "psi" / "vocabulary"
    details: dict[str, Any] = field(default_factory=dict)


# =====================================================================
# DriftDetector
# =====================================================================


class DriftDetector:
    """漂移检测器

    用法：

        detector = DriftDetector(baseline_window=7, threshold=0.2)
        report = detector.detect_numeric_drift(
            "latency",
            baseline_samples=[100, 200, 150],
            current_samples=[300, 400, 350],
        )
        if report.drifted:
            print(f"检测到漂移: {report.drift_score}")

    feature flag 关闭时所有 detect_* 返回 drifted=False 的空报告。
    """

    def __init__(self, baseline_window: int = 7, threshold: float = 0.2) -> None:
        """
        Args:
            baseline_window: 基线窗口天数（用于上下文记录，检测本身不依赖此值）
            threshold: 漂移阈值，drift_score > threshold 触发告警
        """
        self.baseline_window = baseline_window
        self.threshold = threshold

    # ------------------------------------------------------------------
    # 数值漂移检测（简化 KS 检验）
    # ------------------------------------------------------------------

    def detect_numeric_drift(
        self,
        metric_name: str,
        baseline_samples: list[float],
        current_samples: list[float],
    ) -> DriftReport:
        """检测数值漂移（自实现简化 KS 检验）

        KS 检验（Kolmogorov-Smirnov）衡量两个经验分布的最大差距 D。
        简化实现：
          1. 对两组样本分别计算经验 CDF
          2. 取所有可能切分点，求 max(|CDF1(x) - CDF2(x)|)
          3. D > threshold → 漂移

        Args:
            metric_name: 指标名
            baseline_samples: 基线样本列表
            current_samples: 当前样本列表

        Returns:
            DriftReport；feature flag 关闭/样本为空时返回 drifted=False
        """
        if not DRIFT_DETECTION_ENABLED:
            return self._disabled_report(metric_name)

        # 降级：样本为空或过少
        if not baseline_samples or not current_samples:
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="ks",
                details={"reason": "samples_empty"},
            )
        if len(baseline_samples) < 2 or len(current_samples) < 2:
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="ks",
                details={"reason": "samples_too_few"},
            )

        try:
            baseline = [float(x) for x in baseline_samples]
            current = [float(x) for x in current_samples]

            baseline_mean = sum(baseline) / len(baseline)
            current_mean = sum(current) / len(current)

            # 简化 KS：合并所有样本作为切分点，求 max |CDF1 - CDF2|
            all_values = sorted(set(baseline + current))
            n1 = len(baseline)
            n2 = len(current)
            max_diff = 0.0
            for v in all_values:
                cdf1 = sum(1 for x in baseline if x <= v) / n1
                cdf2 = sum(1 for x in current if x <= v) / n2
                diff = abs(cdf1 - cdf2)
                if diff > max_diff:
                    max_diff = diff

            drifted = max_diff > self.threshold

            return DriftReport(
                metric_name=metric_name,
                baseline_value=baseline_mean,
                current_value=current_mean,
                drift_score=max_diff,
                drifted=drifted,
                threshold=self.threshold,
                method="ks",
                details={
                    "baseline_count": n1,
                    "current_count": n2,
                    "baseline_mean": baseline_mean,
                    "current_mean": current_mean,
                },
            )
        except Exception as e:
            logger.warning("数值漂移检测失败 (%s): %s", metric_name, e)
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="ks",
                details={"reason": f"error: {e}"},
            )

    # ------------------------------------------------------------------
    # 分布漂移检测（PSI: Population Stability Index）
    # ------------------------------------------------------------------

    def detect_distribution_drift(
        self,
        metric_name: str,
        baseline_dist: dict[str, float],
        current_dist: dict[str, float],
    ) -> DriftReport:
        """检测分布漂移（用 PSI: Population Stability Index）

        PSI = sum((p_i - q_i) * ln(p_i / q_i))

        其中 p_i 是 baseline 在 bin i 的占比，q_i 是 current 在 bin i 的占比。
        - PSI < 0.1: 无显著漂移
        - 0.1 <= PSI < 0.25: 轻微漂移
        - PSI >= 0.25: 显著漂移

        baseline_dist / current_dist 是 {bin_label: count_or_ratio} 字典。
        若是 count，内部归一化为 ratio；若已是 ratio，直接使用。

        Args:
            metric_name: 指标名
            baseline_dist: {bin: count_or_ratio}
            current_dist: {bin: count_or_ratio}

        Returns:
            DriftReport；feature flag 关闭/分布为空时返回 drifted=False
        """
        if not DRIFT_DETECTION_ENABLED:
            return self._disabled_report(metric_name)

        if not baseline_dist or not current_dist:
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="psi",
                details={"reason": "dist_empty"},
            )

        try:
            # 归一化为 ratio
            b_total = sum(baseline_dist.values()) or 1.0
            c_total = sum(current_dist.values()) or 1.0
            p = {k: float(v) / b_total for k, v in baseline_dist.items()}
            q = {k: float(v) / c_total for k, v in current_dist.items()}

            # 所有 bin 的并集
            all_bins = set(p.keys()) | set(q.keys())
            psi = 0.0
            # 用极小值避免 log(0) / 除零
            epsilon = 1e-10
            for b in all_bins:
                p_i = max(p.get(b, 0.0), epsilon)
                q_i = max(q.get(b, 0.0), epsilon)
                psi += (p_i - q_i) * math.log(p_i / q_i)

            drifted = psi > self.threshold

            # baseline_value / current_value 用最大占比 bin 的标签作为代表
            b_value = max(p.items(), key=lambda x: x[1])[0] if p else ""
            c_value = max(q.items(), key=lambda x: x[1])[0] if q else ""

            return DriftReport(
                metric_name=metric_name,
                baseline_value=float(p.get(str(b_value), 0.0)),
                current_value=float(q.get(str(c_value), 0.0)),
                drift_score=psi,
                drifted=drifted,
                threshold=self.threshold,
                method="psi",
                details={
                    "baseline_top_bin": str(b_value),
                    "current_top_bin": str(c_value),
                    "all_bins": sorted(all_bins),
                },
            )
        except Exception as e:
            logger.warning("分布漂移检测失败 (%s): %s", metric_name, e)
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="psi",
                details={"reason": f"error: {e}"},
            )

    # ------------------------------------------------------------------
    # 文本漂移检测（词汇分布偏移）
    # ------------------------------------------------------------------

    def detect_text_drift(
        self,
        metric_name: str,
        baseline_texts: list[str],
        current_texts: list[str],
    ) -> DriftReport:
        """检测文本漂移（用词汇分布偏移）

        简化方法：
          1. 把 baseline / current 文本分词（按空白切分，小写化）
          2. 计算词频分布（term frequency ratio）
          3. 用词频分布做 PSI 检测
          4. drift_score = PSI + 当前独有词占比

        Args:
            metric_name: 指标名
            baseline_texts: 基线文本列表
            current_texts: 当前文本列表

        Returns:
            DriftReport；feature flag 关闭/文本为空时返回 drifted=False
        """
        if not DRIFT_DETECTION_ENABLED:
            return self._disabled_report(metric_name)

        if not baseline_texts or not current_texts:
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="vocabulary",
                details={"reason": "texts_empty"},
            )

        try:
            # 分词 + 词频统计
            baseline_words = self._tokenize_all(baseline_texts)
            current_words = self._tokenize_all(current_texts)

            if not baseline_words or not current_words:
                return DriftReport(
                    metric_name=metric_name,
                    threshold=self.threshold,
                    method="vocabulary",
                    details={"reason": "no_words_after_tokenize"},
                )

            baseline_freq = self._word_freq(baseline_words)
            current_freq = self._word_freq(current_words)

            # 用 PSI 检测词频分布漂移
            psi_report = self.detect_distribution_drift(
                metric_name, baseline_freq, current_freq
            )
            psi_score = psi_report.drift_score

            # 当前独有词占比（current 有 baseline 没有的词的比例）
            baseline_vocab = set(baseline_freq.keys())
            current_vocab = set(current_freq.keys())
            current_only = current_vocab - baseline_vocab
            current_only_ratio = (
                len(current_only) / len(current_vocab) if current_vocab else 0.0
            )

            # 综合 drift_score = PSI + 当前独有词占比
            drift_score = psi_score + current_only_ratio
            drifted = drift_score > self.threshold

            return DriftReport(
                metric_name=metric_name,
                baseline_value=float(len(baseline_vocab)),
                current_value=float(len(current_vocab)),
                drift_score=drift_score,
                drifted=drifted,
                threshold=self.threshold,
                method="vocabulary",
                details={
                    "psi_score": psi_score,
                    "current_only_ratio": current_only_ratio,
                    "baseline_vocab_size": len(baseline_vocab),
                    "current_vocab_size": len(current_vocab),
                    "current_only_words": sorted(list(current_only))[:20],
                },
            )
        except Exception as e:
            logger.warning("文本漂移检测失败 (%s): %s", metric_name, e)
            return DriftReport(
                metric_name=metric_name,
                threshold=self.threshold,
                method="vocabulary",
                details={"reason": f"error: {e}"},
            )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _disabled_report(self, metric_name: str) -> DriftReport:
        """feature flag 关闭时的空报告"""
        return DriftReport(
            metric_name=metric_name,
            threshold=self.threshold,
            drifted=False,
            method="disabled",
            details={"reason": "drift_detection_disabled"},
        )

    @staticmethod
    def _tokenize_all(texts: list[str]) -> list[str]:
        """简单分词：按空白切分 + 小写化"""
        words: list[str] = []
        for text in texts:
            if not isinstance(text, str):
                continue
            # 简单按空白/标点切分
            for token in text.lower().split():
                # 去除常见标点
                cleaned = token.strip(".,!?;:\"'()[]{}。，！？；：")
                if cleaned:
                    words.append(cleaned)
        return words

    @staticmethod
    def _word_freq(words: list[str]) -> dict[str, int]:
        """词频统计"""
        freq: dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        return freq
