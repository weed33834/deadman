"""D34:跨模型共谋检测器(Cross-Model Collusion Detector)。

问题:
    deadman 多 provider 架构(OpenAI / Anthropic / Zhipu / Ollama)在
    多 agent 协作场景下,可能出现"跨模型共谋"(Cross-Model Collusion):

    1. **Same-Provider Bias**:同 provider 的多个 agent 互相认可违规输出
       (因为训练数据相似,模型行为相近)。
    2. **Shared Blindspot**:多 provider 对同一类输入都失败
       (可能训练数据相似 / 都被同种越狱 prompt 攻破)。
    3. **Cross-Endorsement**:provider A 输出被 provider B 认可频率异常高
       (形成"互相背书"循环)。
    4. **Provider Selection Attack**:攻击者选择特定 provider 绕过护栏
       (某 provider 护栏较弱)。
    5. **Cross-Model Jailbreak**:某 provider 越狱后,把越狱输出传给其他
       provider 复用(扩散越狱)。

    与 D25 ConvergenceDetector 的区别:
    - D25 关注"同模型多 agent 收敛"
    - D34 关注"跨模型 / 跨 provider 共谋"(更深层)
    - D34 复用 D25 的相似度算法 + 反制策略,但增加 provider 维度

    生产风险:
    - 用户得到错误共识(多 provider 都说错,反而增强错误可信度)
    - 越狱扩散(单 provider 越狱传播到全 provider)
    - provider 选择攻击(攻击者选择弱点 provider)
    - 商业化责任不清(哪 provider 出错?)

缓解:
    1. **跨 provider 输出模式趋同检测**:不同 provider 输出高度相似 → 告警
    2. **共享盲点检测**:多 provider 对同一类输入都失败 → 告警
    3. **互相认可检测**:provider A 输出被 provider B 认可频率异常高
    4. **provider 偏好检测**:arbiter 长期偏向某 provider → 告警
    5. **provider 行为基线**:每 provider 独立基线,异常告警
    6. **集成 D25**:复用 Jaccard 相似度 + 反制策略

设计:
    - ProviderOutput:带 provider 标识的 agent 输出
    - CollusionPattern:共谋模式
    - CollusionAlert:共谋告警
    - CrossModelCollusionDetector:主检测器

集成:
    debate.py 投票后(多 provider 场景):
        detector = get_cross_model_collusion_detector()
        result = detector.check_cross_provider(
            outputs=[
                ProviderOutput(provider="openai", agent_name="legal", output="..."),
                ProviderOutput(provider="anthropic", agent_name="tax", output="..."),
                ProviderOutput(provider="zhipu", agent_name="estate", output="..."),
            ],
            endorsements={"openai": "anthropic", "anthropic": "openai", "zhipu": "openai"},
        )
        if result.same_provider_bias_detected:
            # 强制至少一个 provider 不同
            ...
        if result.shared_blindspot_detected:
            # 通知 SRE,可能需要 provider 切换
            ...

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# 复用共享文本相似度工具
from ....utils.text_similarity import text_similarity as _text_similarity
from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


# =====================================================================
# 枚举
# =====================================================================

class ModelProvider(str, Enum):
    """provider 标识(可扩展)。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    ZHIPU = "zhipu"
    OLLAMA = "ollama"
    DEEPSEEK = "deepseek"
    GOOGLE = "google"
    BAIDU = "baidu"
    ALIBABA = "alibaba"
    COHERE = "cohere"
    MISTRAL = "mistral"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class CollusionPattern(str, Enum):
    """共谋模式。"""

    NONE = "none"
    SAME_PROVIDER_BIAS = "same_provider_bias"  # 同 provider 互相认可
    SHARED_BLINDSPOT = "shared_blindspot"  # 共享盲点(都失败)
    CROSS_ENDORSEMENT = "cross_endorsement"  # 互相背书
    PROVIDER_BIAS = "provider_bias"  # arbiter 偏向某 provider
    OUTPUT_CONVERGENCE = "output_convergence"  # 跨 provider 输出趋同
    CROSS_PROVIDER_JAILBREAK = "cross_provider_jailbreak"  # 越狱扩散


class AlertSeverity(str, Enum):
    """告警严重度。"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Countermeasure(str, Enum):
    """反制策略。"""

    NONE = "none"
    FORCE_PROVIDER_DIVERSITY = "force_provider_diversity"  # 强制至少 N 个不同 provider
    ROTATE_ARBITER_PROVIDER = "rotate_arbiter_provider"  # 仲裁 provider 轮换
    ISOLATE_FAILED_PROVIDER = "isolate_failed_provider"  # 隔离失败 provider
    ADD_INDEPENDENT_AGENT = "add_independent_agent"  # 增加独立 agent 打破共谋
    REVIEW_WITH_SRE = "review_with_sre"  # 通知 SRE


# =====================================================================
# 数据类
# =====================================================================

@dataclass
class ProviderOutput:
    """带 provider 标识的 agent 输出。"""

    provider: ModelProvider = ModelProvider.UNKNOWN
    agent_name: str = ""
    output: str = ""
    output_hash: str = ""
    # 是否成功(false → 可能是共享盲点)
    success: bool = True
    # 时间戳
    timestamp: float = field(default_factory=time.time)
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.output_hash and self.output:
            self.output_hash = hashlib.sha256(self.output.encode()).hexdigest()[:16]


@dataclass
class CollusionMetrics:
    """共谋指标。"""

    # provider 数量(去重)
    provider_count: int = 0
    # 同 provider 输出比例(0-1,1=所有输出同 provider)
    same_provider_ratio: float = 0.0
    # 跨 provider 输出相似度(平均)
    cross_provider_similarity: float = 0.0
    # 跨 provider 相同 hash 比例
    cross_provider_same_hash_ratio: float = 0.0
    # 失败 provider 比例
    failure_ratio: float = 0.0
    # 互相认可频率(provider A endorsed by B 的频率)
    cross_endorsement_rate: float = 0.0
    # provider 分布熵(越低越偏向某 provider)
    provider_entropy: float = 0.0
    # 仲裁偏好(若 winner_provider 占比 > 0.5)
    arbiter_bias_provider: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CollusionAlert:
    """共谋告警。"""

    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    severity: AlertSeverity = AlertSeverity.WARNING
    pattern: CollusionPattern = CollusionPattern.NONE
    message: str = ""
    metrics: dict = field(default_factory=dict)
    countermeasure: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["pattern"] = self.pattern.value
        return d


@dataclass
class CollusionCheckResult:
    """单次跨模型共谋检测结果。"""

    session_id: str = ""
    metrics: CollusionMetrics = field(default_factory=CollusionMetrics)
    alerts: list[CollusionAlert] = field(default_factory=list)

    # 便捷属性(用于快速判断)
    same_provider_bias_detected: bool = False
    shared_blindspot_detected: bool = False
    cross_endorsement_detected: bool = False
    provider_bias_detected: bool = False
    output_convergence_detected: bool = False
    jailbreak_diffusion_detected: bool = False

    @property
    def has_alerts(self) -> bool:
        return bool(self.alerts)

    @property
    def has_critical_alerts(self) -> bool:
        return any(a.severity == AlertSeverity.CRITICAL for a in self.alerts)

    def add_alert(self, alert: CollusionAlert) -> None:
        self.alerts.append(alert)
        # 自动设置对应标志
        if alert.pattern == CollusionPattern.SAME_PROVIDER_BIAS:
            self.same_provider_bias_detected = True
        elif alert.pattern == CollusionPattern.SHARED_BLINDSPOT:
            self.shared_blindspot_detected = True
        elif alert.pattern == CollusionPattern.CROSS_ENDORSEMENT:
            self.cross_endorsement_detected = True
        elif alert.pattern == CollusionPattern.PROVIDER_BIAS:
            self.provider_bias_detected = True
        elif alert.pattern == CollusionPattern.OUTPUT_CONVERGENCE:
            self.output_convergence_detected = True
        elif alert.pattern == CollusionPattern.CROSS_PROVIDER_JAILBREAK:
            self.jailbreak_diffusion_detected = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        d["alerts"] = [a.to_dict() for a in self.alerts]
        return d


# =====================================================================
# 默认配置
# =====================================================================

DETECTOR_DEFAULTS = {
    # 跨 provider 输出相似度阈值(超过 → 输出趋同告警)
    "cross_provider_similarity_threshold": 0.85,
    # 同 provider 比例阈值(超过 → same_provider_bias 告警)
    "same_provider_ratio_threshold": 0.5,
    # 失败 provider 比例阈值(超过 → shared_blindspot 告警)
    "shared_blindspot_failure_threshold": 0.7,
    # 互相认可频率阈值(超过 → cross_endorsement 告警)
    "cross_endorsement_rate_threshold": 0.6,
    # provider 分布熵下限(低于 → provider_bias 告警)
    "min_provider_entropy": 0.5,
    "min_provider_samples": 3,  # 至少 N 个 provider 才评估熵
    # 越狱扩散检测:相同 hash 跨 provider 出现
    "jailbreak_diffusion_min_providers": 2,
    # 最少输出数(用于检测)
    "min_outputs_for_check": 2,
    # 历史保留
    "history_retention": 1000,
    # arbiter 偏好窗口
    "arbiter_bias_window": 20,
}


# =====================================================================
# Cross-Model Collusion Detector
# =====================================================================

class CrossModelCollusionDetector:
    """跨模型共谋检测器。

    用法:
        detector = get_cross_model_collusion_detector()

        # 1. 辩论后检测
        result = detector.check_cross_provider(
            outputs=[
                ProviderOutput(provider=ModelProvider.OPENAI, agent_name="legal", output="建议按民法典处理"),
                ProviderOutput(provider=ModelProvider.ANTHROPIC, agent_name="tax", output="建议按民法典处理"),
                ProviderOutput(provider=ModelProvider.ZHIPU, agent_name="estate", output="建议按民法典处理"),
            ],
            session_id="sess-1",
        )

        if result.output_convergence_detected:
            # 跨 provider 输出高度相似 → 强制至少一个 provider 不同
            ...
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = {**DETECTOR_DEFAULTS, **(config or {})}
        self._lock = threading.RLock()
        # 历史:用于 arbiter 偏好 / 越狱扩散
        self._winner_history: deque[tuple[str, str]] = deque(maxlen=self.config["history_retention"])
        # provider 输出历史:用于越狱扩散(跨 session)
        self._output_history: deque[tuple[str, str, str]] = deque(maxlen=self.config["history_retention"])
        # 告警历史
        self._alerts: deque[CollusionAlert] = deque(maxlen=self.config["history_retention"])
        # 统计
        self._stats: dict[str, int] = defaultdict(int)

    # ==================================================================
    # 主检测入口
    # ==================================================================

    def check_cross_provider(
        self,
        *,
        outputs: list[ProviderOutput],
        endorsements: dict[str, str] | None = None,
        winner_provider: str | None = None,
        session_id: str = "",
    ) -> CollusionCheckResult:
        """检测跨 provider 共谋。

        Args:
            outputs: 多 provider 输出列表
            endorsements: provider A 信任 / 推荐 provider B 的映射
                (key=A, value=B 表示 A 认可 B 的输出)
            winner_provider: 胜出 provider(用于 arbiter 偏好检测)
            session_id: 会话 ID

        Returns:
            CollusionCheckResult:检测结果
        """
        result = CollusionCheckResult(session_id=session_id)

        if not is_enabled("defense"):
            return result

        if len(outputs) < self.config["min_outputs_for_check"]:
            return result

        with self._lock:
            self._stats["checks"] += 1

        # 1. 计算 metrics
        metrics = self._compute_metrics(outputs, endorsements, winner_provider)
        result.metrics = metrics

        # 2. 检测同 provider 偏向
        if metrics.same_provider_ratio > self.config["same_provider_ratio_threshold"]:
            result.add_alert(CollusionAlert(
                session_id=session_id,
                severity=AlertSeverity.WARNING,
                pattern=CollusionPattern.SAME_PROVIDER_BIAS,
                message=(
                    f"Same provider bias: ratio={metrics.same_provider_ratio:.3f} "
                    f"(threshold={self.config['same_provider_ratio_threshold']})"
                ),
                metrics=metrics.to_dict(),
                countermeasure=Countermeasure.FORCE_PROVIDER_DIVERSITY.value,
            ))
            with self._lock:
                self._stats["same_provider_bias"] += 1

        # 3. 检测输出趋同(跨 provider)
        if metrics.cross_provider_similarity > self.config["cross_provider_similarity_threshold"]:
            severity = (
                AlertSeverity.CRITICAL
                if metrics.cross_provider_similarity > 0.95
                else AlertSeverity.WARNING
            )
            result.add_alert(CollusionAlert(
                session_id=session_id,
                severity=severity,
                pattern=CollusionPattern.OUTPUT_CONVERGENCE,
                message=(
                    f"Output convergence across providers: "
                    f"similarity={metrics.cross_provider_similarity:.3f} "
                    f"(threshold={self.config['cross_provider_similarity_threshold']})"
                ),
                metrics=metrics.to_dict(),
                countermeasure=Countermeasure.ADD_INDEPENDENT_AGENT.value,
            ))
            with self._lock:
                self._stats["output_convergence"] += 1

        # 4. 检测共享盲点
        if (
            metrics.failure_ratio > self.config["shared_blindspot_failure_threshold"]
            and len(outputs) >= self.config["min_provider_samples"]
        ):
            result.add_alert(CollusionAlert(
                session_id=session_id,
                severity=AlertSeverity.CRITICAL,
                pattern=CollusionPattern.SHARED_BLINDSPOT,
                message=(
                    f"Shared blindspot: {metrics.failure_ratio:.1%} providers failed "
                    f"on same input (threshold={self.config['shared_blindspot_failure_threshold']})"
                ),
                metrics=metrics.to_dict(),
                countermeasure=Countermeasure.REVIEW_WITH_SRE.value,
            ))
            with self._lock:
                self._stats["shared_blindspot"] += 1

        # 5. 检测互相背书
        if metrics.cross_endorsement_rate > self.config["cross_endorsement_rate_threshold"]:
            result.add_alert(CollusionAlert(
                session_id=session_id,
                severity=AlertSeverity.WARNING,
                pattern=CollusionPattern.CROSS_ENDORSEMENT,
                message=(
                    f"Cross endorsement: rate={metrics.cross_endorsement_rate:.3f} "
                    f"(threshold={self.config['cross_endorsement_rate_threshold']})"
                ),
                metrics=metrics.to_dict(),
                countermeasure=Countermeasure.ADD_INDEPENDENT_AGENT.value,
            ))
            with self._lock:
                self._stats["cross_endorsement"] += 1

        # 6. 检测 arbiter 偏好(熵低)
        if (
            metrics.arbiter_bias_provider
            and metrics.provider_entropy < self.config["min_provider_entropy"]
            and len(self._winner_history) >= 5
        ):
            result.add_alert(CollusionAlert(
                session_id=session_id,
                severity=AlertSeverity.WARNING,
                pattern=CollusionPattern.PROVIDER_BIAS,
                message=(
                    f"Arbiter provider bias: prefers {metrics.arbiter_bias_provider} "
                    f"(entropy={metrics.provider_entropy:.3f})"
                ),
                metrics=metrics.to_dict(),
                countermeasure=Countermeasure.ROTATE_ARBITER_PROVIDER.value,
            ))
            with self._lock:
                self._stats["provider_bias"] += 1

        # 7. 检测越狱扩散(相同 hash 跨 provider)
        jailbreak_diff = self._check_jailbreak_diffusion(outputs, session_id)
        if jailbreak_diff:
            result.add_alert(jailbreak_diff)
            with self._lock:
                self._stats["jailbreak_diffusion"] += 1

        # 8. 记录历史(winner_provider)
        with self._lock:
            if winner_provider:
                self._winner_history.append((session_id, winner_provider))
            for o in outputs:
                self._output_history.append((o.provider.value, o.output_hash, session_id))
            self._alerts.extend(result.alerts)

        return result

    # ==================================================================
    # 指标计算
    # ==================================================================

    def _compute_metrics(
        self,
        outputs: list[ProviderOutput],
        endorsements: dict[str, str] | None,
        winner_provider: str | None,
    ) -> CollusionMetrics:
        """计算共谋指标。"""
        metrics = CollusionMetrics()

        if not outputs:
            return metrics

        # provider 数量(去重)
        providers = [o.provider.value for o in outputs]
        unique_providers = set(providers)
        metrics.provider_count = len(unique_providers)

        # 同 provider 比例(最高频 provider 占比)
        provider_counter = Counter(providers)
        most_common_count = provider_counter.most_common(1)[0][1] if provider_counter else 0
        metrics.same_provider_ratio = most_common_count / len(outputs)

        # 跨 provider 输出相似度(只对不同 provider 计算两两相似度)
        similarities = []
        same_hash_count = 0
        cross_pairs = 0
        for i, o1 in enumerate(outputs):
            for j, o2 in enumerate(outputs):
                if i >= j:
                    continue
                if o1.provider == o2.provider:
                    continue  # 只算跨 provider
                cross_pairs += 1
                sim = _text_similarity(o1.output, o2.output)
                similarities.append(sim)
                if o1.output_hash == o2.output_hash and o1.output_hash:
                    same_hash_count += 1
        metrics.cross_provider_similarity = (
            sum(similarities) / len(similarities) if similarities else 0.0
        )
        metrics.cross_provider_same_hash_ratio = (
            same_hash_count / cross_pairs if cross_pairs else 0.0
        )

        # 失败 provider 比例
        failed = sum(1 for o in outputs if not o.success)
        metrics.failure_ratio = failed / len(outputs)

        # 互相认可频率
        if endorsements:
            # 计算双向认可(A→B and B→A)
            mutual_count = 0
            for a, b in endorsements.items():
                if b in endorsements and endorsements[b] == a:
                    mutual_count += 1
            # 双向认可除以总认可数
            metrics.cross_endorsement_rate = (
                mutual_count / len(endorsements) if endorsements else 0.0
            )

        # provider 分布熵
        if len(unique_providers) >= self.config["min_provider_samples"]:
            metrics.provider_entropy = self._entropy(providers)

        # arbiter 偏好
        if winner_provider:
            with self._lock:
                recent_winners = [w for _, w in list(self._winner_history)[-self.config["arbiter_bias_window"]:]
                                  if w]
                recent_winners.append(winner_provider)
            if recent_winners:
                winner_counter = Counter(recent_winners)
                most_common_provider, most_common_count = winner_counter.most_common(1)[0]
                if most_common_count / len(recent_winners) > 0.5:
                    metrics.arbiter_bias_provider = most_common_provider

        return metrics

    @staticmethod
    def _entropy(items: list[str]) -> float:
        """计算 Shannon 熵(以 log2 为底)。"""
        if not items:
            return 0.0
        counter = Counter(items)
        total = len(items)
        entropy = 0.0
        for count in counter.values():
            p = count / total
            if p > 0:
                entropy -= p * math.log2(p)
        return entropy

    # ==================================================================
    # 越狱扩散检测
    # ==================================================================

    def _check_jailbreak_diffusion(
        self,
        outputs: list[ProviderOutput],
        session_id: str,
    ) -> CollusionAlert | None:
        """检测越狱扩散:相同 output_hash 跨 provider(跨 session)出现。

        若同一 hash 在多个 provider 的输出中出现(可能跨 session),
        且 hash 不在历史正常输出集合中 → 越狱扩散告警。
        """
        # 当前 outputs 中相同 hash 的 provider 数
        hash_to_providers: dict[str, set[str]] = defaultdict(set)
        for o in outputs:
            if o.output_hash:
                hash_to_providers[o.output_hash].add(o.provider.value)

        # 找跨 provider 相同 hash(当前 session)
        for h, provs in hash_to_providers.items():
            if len(provs) >= self.config["jailbreak_diffusion_min_providers"]:
                return CollusionAlert(
                    session_id=session_id,
                    severity=AlertSeverity.CRITICAL,
                    pattern=CollusionPattern.CROSS_PROVIDER_JAILBREAK,
                    message=(
                        f"Cross-provider jailbreak diffusion: hash {h} appears in "
                        f"{len(provs)} providers: {provs}"
                    ),
                    metrics={
                        "hash": h,
                        "providers": list(provs),
                    },
                    countermeasure=Countermeasure.ISOLATE_FAILED_PROVIDER.value,
                )
        return None

    # ==================================================================
    # 查询 / 看板
    # ==================================================================

    def get_winner_distribution(self) -> dict[str, int]:
        """获取 winner provider 分布(用于看板)。"""
        with self._lock:
            winners = [w for _, w in self._winner_history]
        return dict(Counter(winners))

    def get_provider_distribution(self) -> dict[str, int]:
        """获取输出 provider 分布。"""
        with self._lock:
            providers = [p for p, _, _ in self._output_history]
        return dict(Counter(providers))

    def get_recent_alerts(self, limit: int = 100) -> list[CollusionAlert]:
        with self._lock:
            return list(self._alerts)[-limit:]

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
            stats["total_winners"] = len(self._winner_history)
            stats["total_outputs"] = len(self._output_history)
            stats["total_alerts"] = len(self._alerts)
            return stats


# =====================================================================
# 全局单例
# =====================================================================

_detector_instance: CrossModelCollusionDetector | None = None
_detector_lock = threading.RLock()


def get_cross_model_collusion_detector() -> CrossModelCollusionDetector:
    """获取全局 CrossModelCollusionDetector 单例。"""
    global _detector_instance
    with _detector_lock:
        if _detector_instance is None:
            _detector_instance = CrossModelCollusionDetector()
        return _detector_instance


def reset_cross_model_collusion_detector() -> None:
    """重置全局单例(测试用)。"""
    global _detector_instance
    with _detector_lock:
        _detector_instance = None


__all__ = [
    "AlertSeverity",
    "CollusionAlert",
    "CollusionCheckResult",
    "CollusionMetrics",
    "CollusionPattern",
    "Countermeasure",
    "CrossModelCollusionDetector",
    "ModelProvider",
    "ProviderOutput",
    "get_cross_model_collusion_detector",
    "reset_cross_model_collusion_detector",
]
