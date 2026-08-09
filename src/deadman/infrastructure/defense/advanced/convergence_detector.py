"""D25:多智能体收敛检测器(Multi-agent Convergence Detector)。

问题:
    deadman 多智能体架构(6 个 agent + 12 子 agent)在 debate / vote / 协作场景下,
    可能出现"共谋"或"回声室"现象:

    1. **Echo Chamber**:agent A 输出 → 影响 agent B 输出 → 反过来强化 agent A,形成回声室
    2. **Collusion**:多 agent 长期共享 Reflexion 策略,形成"集体偏见"(都认可某错误模式)
    3. **Convergence Collapse**:多 agent 答案趋同(原本应有差异化观点),丧失多样性
    4. **Cascade Failure**:agent A 失败 → agent B 基于 A 的输出也失败 → 级联错误
    5. **Arbiter Bias**:仲裁 agent 长期偏向某 agent 输出(形成偏好)

    生产风险:
    - 用户得到错误共识(多个 agent 都说错,反而增强错误可信度)
    - 辩论失去意义(变成"互相同意")
    - 仲裁失效(总选同一个 agent)
    - 难以发现(表面看是"高一致性",实则是"无多样性")

缓解:
    1. **答案多样性度量**:计算多 agent 答案的语义距离 / 编辑距离 / Jaccard 相似度
    2. **共识阈值告警**:共识度过高(>0.95)且无对立观点 → 告警
    3. **Agent 偏好检测**:仲裁 agent 的选择分布(检测是否总选某一个)
    4. **跨 agent 反思污染**:Reflexion 策略共享前的多样性预检
    5. **强制对立机制**:检测到回声室 → 强制要求某 agent 提供"反对意见"

设计:
    - AgentOutput: 单个 agent 输出
    - ConvergenceMetrics: 多样性 / 共识度 / 偏好分布
    - ConvergenceDetector: 主检测器
    - 反制策略:强制对立 / 隔离 / 重置

集成:
    debate.py 投票后:
        detector = get_convergence_detector()
        result = detector.check_debate(
            agent_outputs=[agent_a_answer, agent_b_answer, agent_c_answer],
            votes={"agent_a": 0, "agent_b": 1, "agent_c": 2},  # 各 agent 投谁
            winner="agent_b",
            session_id="sess-1",
        )
        if result.echo_chamber_detected:
            # 强制要求重新辩论,要求至少一个 agent 提供反对意见
            ...
        if result.arbiter_bias_detected:
            # 仲裁偏好 → 切换仲裁 agent
            ...

feature flag:`DEADMAN_DEFENSE_ENABLED=1` 默认启用。
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ....utils.text_similarity import jaccard_similarity, tokenize
from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


# =====================================================================
# 数据类
# =====================================================================


class AlertSeverity(str, Enum):
    """告警严重度。"""

    INFO = "info"  # 信息性(无需处理)
    WARNING = "warning"  # 警告(观察)
    CRITICAL = "critical"  # 严重(必须干预)


class AntiPattern(str, Enum):
    """检测到的反模式。"""

    NONE = "none"
    ECHO_CHAMBER = "echo_chamber"  # 回声室:输出高度相似
    CONVERGENCE_COLLAPSE = "convergence_collapse"  # 共识崩塌:共识度过高且无对立
    ARBITER_BIAS = "arbiter_bias"  # 仲裁偏好:总选同一个 agent
    CASCADE_FAILURE = "cascade_failure"  # 级联失败:多 agent 同时失败
    REFLEXION_POLLUTION = "reflexion_pollution"  # 反思污染:策略导致集体偏见
    LOW_DIVERSITY = "low_diversity"  # 多样性不足


@dataclass
class AgentOutput:
    """单次 agent 输出记录。"""

    agent_name: str
    output: str
    # 输出指纹(hash,用于快速比较)
    output_hash: str = ""
    # 是否成功
    success: bool = True
    # 输出 token 数(用于相关性判断)
    output_tokens: int = 0
    # 时间戳
    timestamp: float = field(default_factory=time.time)
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.output_hash and self.output:
            self.output_hash = hashlib.sha256(self.output.encode()).hexdigest()[:16]
        if not self.output_tokens and self.output:
            # 粗略估算:中英文混合,字符数 / 2
            self.output_tokens = max(1, len(self.output) // 2)


@dataclass
class ConvergenceMetrics:
    """收敛指标。"""

    # 文本相似度矩阵(agent_name x agent_name)
    pairwise_similarity: dict[str, dict[str, float]] = field(default_factory=dict)
    # 平均相似度(0-1,1=完全相同)
    avg_similarity: float = 0.0
    # 最大相似度
    max_similarity: float = 0.0
    # 多样性得分(1 - avg_similarity,0=无多样性,1=完全不同)
    diversity_score: float = 1.0
    # 共识度(支持 winner 的 agent 占比)
    consensus_ratio: float = 0.0
    # 是否有反对意见(至少一个 agent 不同意 winner)
    has_dissent: bool = True
    # 唯一 hash 数 / 总数
    unique_ratio: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConvergenceAlert:
    """收敛告警。"""

    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    severity: AlertSeverity = AlertSeverity.WARNING
    pattern: AntiPattern = AntiPattern.NONE
    message: str = ""
    metrics: dict = field(default_factory=dict)
    # 反制建议
    countermeasure: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["pattern"] = self.pattern.value
        return d


# =====================================================================
# 文本相似度计算 - 使用共享 text_similarity 模块
# =====================================================================


# =====================================================================
# Convergence Detector
# =====================================================================

# 默认配置
DETECTOR_DEFAULTS = {
    # 回声室阈值:平均相似度 > 此值 → 告警
    "echo_chamber_similarity_threshold": 0.85,
    # 共识崩塌阈值:共识度 > 此值且无反对意见 → 告警
    "convergence_collapse_consensus_threshold": 0.95,
    # 多样性下限:diversity_score < 此值 → 告警
    "min_diversity_score": 0.15,
    # 唯一性下限:unique_ratio < 此值 → 告警(多个 agent 输出完全相同)
    "min_unique_ratio": 0.34,  # 至少 1/3 不同
    # 仲裁偏好:最近 N 次辩论中 winner 分布的熵
    "arbiter_bias_window": 20,
    "arbiter_bias_min_entropy": 1.0,  # 熵低于此值 → 偏好告警
    "arbiter_bias_min_samples": 5,  # 至少 N 次才评估
    # 级联失败:连续 N 个 agent 同时失败
    "cascade_failure_threshold": 2,
    # Reflexion 污染:相同 failure_type 出现频率
    "reflexion_pollution_threshold": 0.7,
    "reflexion_pollution_min_samples": 5,
    # 历史保留(用于趋势分析)
    "history_retention": 1000,
}


class ConvergenceDetector:
    """多智能体收敛检测器。

    用法:
        detector = get_convergence_detector()

        # 1. 辩论后检测
        result = detector.check_debate(
            agent_outputs=[
                AgentOutput(agent_name="legal_advisor", output="..."),
                AgentOutput(agent_name="tax_advisor", output="..."),
                AgentOutput(agent_name="estate_planner", output="..."),
            ],
            votes={"legal_advisor": 0, "tax_advisor": 1, "estate_planner": 2},
            winner="tax_advisor",
            session_id="sess-123",
        )

        if result.echo_chamber_detected:
            # 触发反制:强制重新辩论,要求至少一个 agent 反对
            ...

        # 2. 跨会话检测仲裁偏好
        bias = detector.check_arbiter_bias(session_id="sess-123")
        if bias.arbiter_bias_detected:
            # 切换仲裁 agent
            ...
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = {**DETECTOR_DEFAULTS, **(config or {})}
        self._lock = threading.RLock()
        # 辩论历史(用于跨会话趋势分析)
        self._debate_history: deque[dict] = deque(maxlen=self.config["history_retention"])
        # winner 分布(用于仲裁偏好检测)
        self._winner_history: deque[str] = deque(maxlen=self.config["arbiter_bias_window"])
        # Reflexion failure_type 分布
        self._reflexion_history: deque[str] = deque(maxlen=self.config["history_retention"])
        # agent 输出缓存(token 集合,避免重复 tokenize)
        self._token_cache: dict[str, set[str]] = {}
        # 统计
        self._stats = {
            "checks": 0,
            "echo_chamber": 0,
            "convergence_collapse": 0,
            "arbiter_bias": 0,
            "cascade_failure": 0,
            "reflexion_pollution": 0,
            "low_diversity": 0,
        }
        # 告警历史
        self._alerts: deque[dict] = deque(maxlen=self.config["history_retention"])

    # ---------------- 主检测入口 ----------------

    def check_debate(
        self,
        *,
        agent_outputs: list[AgentOutput],
        votes: dict[str, int] | None = None,
        winner: str | None = None,
        session_id: str = "",
    ) -> ConvergenceCheckResult:
        """检测单次辩论的收敛情况。

        Args:
            agent_outputs: 各 agent 的输出列表
            votes: 各 agent 的投票(agent_name → 投票对象的 idx)
            winner: 胜出的 agent
            session_id: 会话 ID(用于跨会话追踪)

        Returns:
            ConvergenceCheckResult: 检测结果 + 反制建议
        """
        result = ConvergenceCheckResult(session_id=session_id)

        if not is_enabled("defense"):
            return result

        with self._lock:
            self._stats["checks"] += 1

        # 1. 计算收敛指标
        metrics = self._compute_metrics(agent_outputs, votes, winner)
        result.metrics = metrics

        # 2. 检测回声室
        if metrics.avg_similarity > self.config["echo_chamber_similarity_threshold"]:
            result.echo_chamber_detected = True
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.WARNING,
                    pattern=AntiPattern.ECHO_CHAMBER,
                    message=(
                        f"Echo chamber detected: avg_similarity={metrics.avg_similarity:.3f} "
                        f"> threshold {self.config['echo_chamber_similarity_threshold']}"
                    ),
                    metrics=metrics.to_dict(),
                    countermeasure="force_dissent",
                )
            )
            with self._lock:
                self._stats["echo_chamber"] += 1

        # 3. 检测共识崩塌
        if (
            metrics.consensus_ratio >= self.config["convergence_collapse_consensus_threshold"]
            and not metrics.has_dissent
        ):
            result.convergence_collapse_detected = True
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.CRITICAL,
                    pattern=AntiPattern.CONVERGENCE_COLLAPSE,
                    message=(
                        f"Convergence collapse: consensus_ratio={metrics.consensus_ratio:.3f}, "
                        f"no dissent"
                    ),
                    metrics=metrics.to_dict(),
                    countermeasure="force_dissent",
                )
            )
            with self._lock:
                self._stats["convergence_collapse"] += 1

        # 4. 检测多样性不足
        if metrics.diversity_score < self.config["min_diversity_score"]:
            result.low_diversity_detected = True
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.WARNING,
                    pattern=AntiPattern.LOW_DIVERSITY,
                    message=(
                        f"Low diversity: diversity_score={metrics.diversity_score:.3f} "
                        f"< threshold {self.config['min_diversity_score']}"
                    ),
                    metrics=metrics.to_dict(),
                    countermeasure="force_dissent",
                )
            )
            with self._lock:
                self._stats["low_diversity"] += 1

        # 5. 检测完全相同输出(unique_ratio 过低)
        if metrics.unique_ratio < self.config["min_unique_ratio"]:
            result.echo_chamber_detected = True
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.CRITICAL,
                    pattern=AntiPattern.ECHO_CHAMBER,
                    message=(
                        f"Multiple agents produced identical output: unique_ratio="
                        f"{metrics.unique_ratio:.3f}"
                    ),
                    metrics=metrics.to_dict(),
                    countermeasure="force_dissent",
                )
            )

        # 6. 检测级联失败
        failed_agents = [o for o in agent_outputs if not o.success]
        if len(failed_agents) >= self.config["cascade_failure_threshold"]:
            result.cascade_failure_detected = True
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.CRITICAL,
                    pattern=AntiPattern.CASCADE_FAILURE,
                    message=(
                        f"Cascade failure: {len(failed_agents)}/{len(agent_outputs)} "
                        f"agents failed simultaneously"
                    ),
                    metrics={"failed_agents": [a.agent_name for a in failed_agents]},
                    countermeasure="isolate_failed",
                )
            )
            with self._lock:
                self._stats["cascade_failure"] += 1

        # 7. 更新 winner 历史(用于仲裁偏好检测)
        if winner:
            with self._lock:
                self._winner_history.append(winner)

        # 8. 记录辩论历史
        with self._lock:
            self._debate_history.append(
                {
                    "timestamp": time.time(),
                    "session_id": session_id,
                    "agent_count": len(agent_outputs),
                    "winner": winner or "",
                    "avg_similarity": metrics.avg_similarity,
                    "diversity_score": metrics.diversity_score,
                    "consensus_ratio": metrics.consensus_ratio,
                }
            )

        # 9. 检测仲裁偏好(若有足够历史)
        bias = self.check_arbiter_bias(session_id=session_id)
        if bias.arbiter_bias_detected:
            result.arbiter_bias_detected = True
            result.add_alert(
                bias.alerts[0]
                if bias.alerts
                else ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.WARNING,
                    pattern=AntiPattern.ARBITER_BIAS,
                    message="Arbiter bias detected",
                    countermeasure="rotate_arbiter",
                )
            )
            with self._lock:
                self._stats["arbiter_bias"] += 1

        return result

    def check_arbiter_bias(self, *, session_id: str = "") -> ConvergenceCheckResult:
        """检测仲裁偏好(跨会话)。

        通过 winner 分布的熵判断:
        - 熵高 → 分布均匀(无偏好)
        - 熵低 → 总是选某 agent(有偏好)
        """
        result = ConvergenceCheckResult(session_id=session_id)

        if not is_enabled("defense"):
            return result

        with self._lock:
            history = list(self._winner_history)

        if len(history) < self.config["arbiter_bias_min_samples"]:
            return result

        # 计算 winner 分布
        counter = Counter(history)
        n = len(history)
        entropy = 0.0
        for count in counter.values():
            if count > 0:
                p = count / n
                entropy -= p * math.log2(p)

        # 熵低于阈值 → 偏好告警
        if entropy < self.config["arbiter_bias_min_entropy"]:
            result.arbiter_bias_detected = True
            most_common = counter.most_common(1)[0]
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.WARNING,
                    pattern=AntiPattern.ARBITER_BIAS,
                    message=(
                        f"Arbiter bias: entropy={entropy:.3f} < "
                        f"{self.config['arbiter_bias_min_entropy']}, "
                        f"winner '{most_common[0]}' chosen {most_common[1]}/{n} times"
                    ),
                    metrics={
                        "entropy": entropy,
                        "winner_distribution": dict(counter),
                        "total_debates": n,
                    },
                    countermeasure="rotate_arbiter",
                )
            )

        return result

    def check_reflexion_pollution(
        self,
        *,
        failure_types: list[str],
        session_id: str = "",
    ) -> ConvergenceCheckResult:
        """检测 Reflexion 策略污染(集体偏见)。

        若多个 agent 共享 Reflexion 策略且 failure_type 高度集中,
        说明可能形成集体偏见。

        Args:
            failure_types: 各 agent 的最近 failure_type 列表
        """
        result = ConvergenceCheckResult(session_id=session_id)

        if not is_enabled("defense"):
            return result

        if not failure_types:
            return result

        with self._lock:
            for ft in failure_types:
                self._reflexion_history.append(ft)

        if len(failure_types) < self.config["reflexion_pollution_min_samples"]:
            return result

        counter = Counter(failure_types)
        n = len(failure_types)
        most_common_type, most_common_count = counter.most_common(1)[0]
        ratio = most_common_count / n

        if ratio > self.config["reflexion_pollution_threshold"]:
            result.reflexion_pollution_detected = True
            result.add_alert(
                ConvergenceAlert(
                    session_id=session_id,
                    severity=AlertSeverity.WARNING,
                    pattern=AntiPattern.REFLEXION_POLLUTION,
                    message=(
                        f"Reflexion pollution: failure_type '{most_common_type}' "
                        f"appears in {ratio:.1%} of agents (>{self.config['reflexion_pollution_threshold']:.0%})"
                    ),
                    metrics={
                        "failure_type_distribution": dict(counter),
                        "ratio": ratio,
                    },
                    countermeasure="diversify_reflexion",
                )
            )
            with self._lock:
                self._stats["reflexion_pollution"] += 1

        return result

    # ---------------- 查询接口 ----------------

    def get_stats(self) -> dict:
        """获取检测统计。"""
        with self._lock:
            return dict(self._stats)

    def get_recent_alerts(self, limit: int = 50) -> list[dict]:
        """获取最近告警(看板用)。"""
        with self._lock:
            return list(self._alerts)[-limit:]

    def get_winner_distribution(self) -> dict:
        """获取 winner 分布(看板用)。"""
        with self._lock:
            counter = Counter(self._winner_history)
            return {
                "distribution": dict(counter),
                "total": len(self._winner_history),
            }

    def reset(self) -> None:
        """重置状态(测试 / 运维用)。"""
        with self._lock:
            self._debate_history.clear()
            self._winner_history.clear()
            self._reflexion_history.clear()
            self._token_cache.clear()
            self._alerts.clear()
            for k in self._stats:
                self._stats[k] = 0

    # ---------------- 内部 ----------------

    def _compute_metrics(
        self,
        agent_outputs: list[AgentOutput],
        votes: dict[str, int] | None,
        winner: str | None,
    ) -> ConvergenceMetrics:
        """计算收敛指标。"""
        metrics = ConvergenceMetrics()

        n = len(agent_outputs)
        if n < 2:
            metrics.consensus_ratio = 1.0 if winner else 0.0
            metrics.has_dissent = False
            return metrics

        # 1. 计算 pairwise 相似度
        tokenized = []
        for output in agent_outputs:
            if output.output_hash in self._token_cache:
                tokens = self._token_cache[output.output_hash]
            else:
                tokens = tokenize(output.output)
                with self._lock:
                    self._token_cache[output.output_hash] = tokens
            tokenized.append((output.agent_name, tokens))

        similarities = []
        for i in range(n):
            metrics.pairwise_similarity.setdefault(tokenized[i][0], {})
            for j in range(i + 1, n):
                sim = jaccard_similarity(tokenized[i][1], tokenized[j][1])
                metrics.pairwise_similarity[tokenized[i][0]][tokenized[j][0]] = sim
                metrics.pairwise_similarity.setdefault(tokenized[j][0], {})[tokenized[i][0]] = sim
                similarities.append(sim)

        if similarities:
            metrics.avg_similarity = sum(similarities) / len(similarities)
            metrics.max_similarity = max(similarities)

        metrics.diversity_score = 1.0 - metrics.avg_similarity

        # 2. 唯一性(unique_ratio)
        unique_hashes = {o.output_hash for o in agent_outputs if o.output}
        metrics.unique_ratio = len(unique_hashes) / n if n > 0 else 1.0

        # 3. 共识度(支持 winner 的 agent 占比)
        # votes 是 {voter: voted_for_idx},但 winner 是 agent_name
        # 简化:计算有多少 agent 的输出与 winner 相同(hash)
        if winner:
            winner_output = next((o for o in agent_outputs if o.agent_name == winner), None)
            if winner_output:
                same_count = sum(
                    1 for o in agent_outputs if o.output_hash == winner_output.output_hash
                )
                metrics.consensus_ratio = same_count / n
                # 是否有反对意见(至少一个 agent 输出与 winner 不同)
                metrics.has_dissent = same_count < n
            else:
                metrics.has_dissent = True

        return metrics

    def _record_alert(self, alert: ConvergenceAlert) -> None:
        with self._lock:
            self._alerts.append(alert.to_dict())


@dataclass
class ConvergenceCheckResult:
    """收敛检测结果。"""

    session_id: str = ""
    metrics: ConvergenceMetrics = field(default_factory=ConvergenceMetrics)
    # 检测到的反模式
    echo_chamber_detected: bool = False
    convergence_collapse_detected: bool = False
    arbiter_bias_detected: bool = False
    cascade_failure_detected: bool = False
    reflexion_pollution_detected: bool = False
    low_diversity_detected: bool = False
    # 告警列表
    alerts: list[ConvergenceAlert] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return any(
            [
                self.echo_chamber_detected,
                self.convergence_collapse_detected,
                self.arbiter_bias_detected,
                self.cascade_failure_detected,
                self.reflexion_pollution_detected,
                self.low_diversity_detected,
            ]
        )

    @property
    def highest_severity(self) -> AlertSeverity:
        if not self.alerts:
            return AlertSeverity.INFO
        severity_order = [AlertSeverity.INFO, AlertSeverity.WARNING, AlertSeverity.CRITICAL]
        return max(self.alerts, key=lambda a: severity_order.index(a.severity)).severity

    @property
    def recommended_countermeasures(self) -> list[str]:
        """推荐反制措施(去重)。"""
        return list({a.countermeasure for a in self.alerts if a.countermeasure})

    def add_alert(self, alert: ConvergenceAlert) -> None:
        self.alerts.append(alert)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "metrics": self.metrics.to_dict(),
            "echo_chamber_detected": self.echo_chamber_detected,
            "convergence_collapse_detected": self.convergence_collapse_detected,
            "arbiter_bias_detected": self.arbiter_bias_detected,
            "cascade_failure_detected": self.cascade_failure_detected,
            "reflexion_pollution_detected": self.reflexion_pollution_detected,
            "low_diversity_detected": self.low_diversity_detected,
            "has_issues": self.has_issues,
            "highest_severity": self.highest_severity.value,
            "recommended_countermeasures": self.recommended_countermeasures,
            "alerts": [a.to_dict() for a in self.alerts],
        }


# =====================================================================
# 反制策略(可选,供调用方使用)
# =====================================================================


class CountermeasureStrategy:
    """反制策略(静态方法,供调用方按需使用)。

    用法:
        if result.echo_chamber_detected:
            CountermeasureStrategy.force_dissent(debate_context)
    """

    @staticmethod
    def force_dissent(agent_names: list[str], winner: str) -> dict:
        """强制对立:要求某个非 winner agent 提供反对意见。

        Returns:
            dict: 含 dissent_agent / dissent_prompt
        """
        candidates = [a for a in agent_names if a != winner]
        if not candidates:
            return {"dissent_agent": None, "dissent_prompt": ""}

        # 简化:选第一个非 winner
        dissent_agent = candidates[0]
        return {
            "dissent_agent": dissent_agent,
            "dissent_prompt": (
                f"请提出与 {winner} 不同的观点。即使你认同结论,也请提出至少一个反对视角 / 边界情况 / "
                f"潜在风险,以确保辩论的多样性和严谨性。"
            ),
        }

    @staticmethod
    def rotate_arbiter(current_arbiter: str, candidates: list[str]) -> str:
        """轮换仲裁:返回下一个仲裁 agent。

        简单轮询,确保不会总是同一个 agent 仲裁。
        """
        if not candidates:
            return current_arbiter
        if current_arbiter in candidates:
            idx = candidates.index(current_arbiter)
            return candidates[(idx + 1) % len(candidates)]
        return candidates[0]

    @staticmethod
    def isolate_failed(failed_agents: list[str]) -> dict:
        """隔离失败 agent:返回降级方案。"""
        return {
            "isolated_agents": failed_agents,
            "action": "skip_failed_and_use_cached",
            "fallback": "use_single_agent_or_lookup",
        }

    @staticmethod
    def diversify_reflexion(agents_sharing_strategy: list[str]) -> dict:
        """分散 Reflexion:对共享策略的 agent 重新分配独立策略。"""
        return {
            "affected_agents": agents_sharing_strategy,
            "action": "split_reflexion_strategy",
            "new_assignment": {
                agent: f"independent_strategy_{i}"
                for i, agent in enumerate(agents_sharing_strategy)
            },
        }


# =====================================================================
# 全局单例
# =====================================================================

_detector_instance: ConvergenceDetector | None = None
_detector_lock = threading.RLock()


def get_convergence_detector() -> ConvergenceDetector:
    """获取全局 ConvergenceDetector 单例。"""
    global _detector_instance
    with _detector_lock:
        if _detector_instance is None:
            _detector_instance = ConvergenceDetector()
        return _detector_instance


def reset_convergence_detector() -> None:
    """重置全局单例(测试用)。"""
    global _detector_instance
    with _detector_lock:
        _detector_instance = None


__all__ = [
    "AgentOutput",
    "AlertSeverity",
    "AntiPattern",
    "ConvergenceAlert",
    "ConvergenceCheckResult",
    "ConvergenceDetector",
    "ConvergenceMetrics",
    "CountermeasureStrategy",
    "get_convergence_detector",
    "reset_convergence_detector",
]
