"""D33:宪法漂移检测器(Constitutional Drift Detector)。

问题:
    deadman 护栏系统(L0-L8 规则链 + P5 安全护栏 + reasoning 护栏)长期运行后,
    可能出现"宪法漂移"(Constitutional Drift):

    1. **每月微调累积**:运维 / PM 每月根据线上反馈微调护栏阈值
       (如 confidence_threshold 从 0.8 → 0.79 → 0.78 ...),单次看似无害,
       一年后累积到 0.65,远低于基线,护栏失效。
    2. **策略漂移**:护栏策略从"严格阻断"逐步改为"警告 + 透传",
       长期后护栏变成"软建议"。
    3. **多区域配置漂移**(v1.7 P5 已识别):多区域部署,护栏配置不同步。
    4. **配置回滚血泪**:配置变更出错,回滚后又出错(version skip)。
    5. **审计盲区**:无历史追踪,事后无法追溯"何时开始漂移"。

    生产风险:
    - 护栏长期失效(漂移后无法拦截违规)
    - 合规违规(法规要求特定阈值)
    - 故障难复现(不知道阈值何时变了)
    - 责任不清(谁调的?为什么调?)

缓解:
    1. **阈值历史追踪**:每次阈值变更记录快照(timestamp + value + actor + reason)
    2. **基线比对**:与"基线快照"(初始 / 上次 review)比对,计算漂移
    3. **漂移告警**:绝对漂移 / 相对漂移率超阈值 → 告警
    4. **趋势分析**:单调漂移检测(连续 N 次同向 → 告警)
    5. **审计接口**:list_thresholds / get_drift_history / get_alerts
    6. **回滚建议**:critical 漂移 → 建议回滚到基线

设计:
    - ThresholdSnapshot:阈值快照
    - DriftAlert:漂移告警
    - ConstitutionalDriftDetector:主检测器(线程安全 + 可持久化)

集成:
    config 更新时(guardrails.py / safety_settings.py):
        detector = get_constitutional_drift_detector()
        detector.record_threshold(
            name="confidence_threshold",
            value=0.78,
            actor="ops-alice",
            reason="减少误杀率(line 7 月反馈)",
        )

    定期(每周 / 每月)审查:
        report = detector.get_drift_report()
        if report.has_critical_alerts:
            # 通知 SRE / 安全团队 review
            ...

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


# =====================================================================
# 枚举
# =====================================================================

class ThresholdType(str, Enum):
    """阈值类型(决定基线 / 漂移容忍度)。"""

    NUMERIC = "numeric"  # 数值(如 0.8)
    ENUM = "enum"  # 枚举(如 strict / warn / passthrough)
    BOOLEAN = "boolean"  # 布尔


class DriftSeverity(str, Enum):
    """漂移严重度。"""

    NONE = "none"
    ACCEPTABLE = "acceptable"  # < 10%,正常波动
    CONCERNING = "concerning"  # 10-30%,需关注
    CRITICAL = "critical"  # > 30%,必须 review


class DriftDirection(str, Enum):
    """漂移方向(单调趋势分析)。"""

    NONE = "none"
    UP = "up"  # 数值上升
    DOWN = "down"  # 数值下降
    OSCILLATING = "oscillating"  # 振荡


class ChangeReason(str, Enum):
    """变更原因(用于审计)。"""

    MANUAL_TUNING = "manual_tuning"  # 人工调参
    INCIDENT_RESPONSE = "incident_response"  # 事故响应
    A_B_TEST = "a_b_test"  # A/B 测试
    COMPLIANCE_UPDATE = "compliance_update"  # 合规更新
    MODEL_UPGRADE = "model_upgrade"  # 模型升级
    ROLLBACK = "rollback"  # 回滚
    UNKNOWN = "unknown"


# =====================================================================
# 数据类
# =====================================================================

@dataclass
class ThresholdSnapshot:
    """阈值快照(每次变更记录一条)。

    包含:
    - timestamp:变更时间
    - name:阈值名(如 confidence_threshold)
    - value:新值(JSON-serialized,支持数值 / 枚举 / 布尔)
    - prev_value:上一值
    - type:阈值类型
    - actor:操作者(user / system)
    - reason:变更原因
    - tags:额外标签(如 service / region)
    - hash:快照 hash(防篡改)
    """

    timestamp: float = field(default_factory=time.time)
    name: str = ""
    value: Any = None  # JSON-serializable
    prev_value: Any = None
    type: ThresholdType = ThresholdType.NUMERIC
    actor: str = "system"
    reason: ChangeReason = ChangeReason.UNKNOWN
    reason_text: str = ""  # 自由文本说明
    tags: dict[str, str] = field(default_factory=dict)
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        material = (
            f"{self.timestamp}:{self.name}:{self.value}:{self.prev_value}:"
            f"{self.type.value}:{self.actor}:{self.reason.value}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def verify_hash(self) -> bool:
        return self._compute_hash() == self.hash

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["reason"] = self.reason.value
        return d


@dataclass
class DriftAlert:
    """漂移告警。"""

    timestamp: float = field(default_factory=time.time)
    threshold_name: str = ""
    severity: DriftSeverity = DriftSeverity.ACCEPTABLE
    direction: DriftDirection = DriftDirection.NONE
    message: str = ""
    # 当前值 / 基线值
    current_value: Any = None
    baseline_value: Any = None
    # 漂移量(数值:绝对差;枚举:0/1 不同;布尔:0/1 不同)
    absolute_drift: float = 0.0
    relative_drift: float = 0.0  # 百分比(0-1)
    # 连续同向变更次数(单调趋势)
    consecutive_same_direction: int = 0
    # 建议反制
    countermeasure: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["direction"] = self.direction.value
        return d


@dataclass
class DriftReport:
    """漂移报告(定期生成)。"""

    generated_at: float = field(default_factory=time.time)
    baseline_set_at: float = 0.0
    # 当前所有阈值
    current_thresholds: dict[str, Any] = field(default_factory=dict)
    # 漂移告警(按严重度排序)
    alerts: list[DriftAlert] = field(default_factory=list)
    # 总变更次数
    total_changes: int = 0
    # 是否有 critical 告警
    has_critical_alerts: bool = False

    @property
    def has_concerning_alerts(self) -> bool:
        return any(a.severity in (DriftSeverity.CONCERNING, DriftSeverity.CRITICAL) for a in self.alerts)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["alerts"] = [a.to_dict() for a in self.alerts]
        d.pop("has_critical_alerts", None)  # property
        d["has_critical_alerts"] = self.has_critical_alerts
        d["has_concerning_alerts"] = self.has_concerning_alerts
        return d


# =====================================================================
# 默认配置
# =====================================================================

DETECTOR_DEFAULTS = {
    # 漂移告警阈值(相对百分比,符合文档:0-10% ACCEPTABLE,10-30% CONCERNING,>30% CRITICAL)
    "drift_acceptable_threshold": 0.0,  # 任何变化至少 ACCEPTABLE(< 10%)
    "drift_concerning_threshold": 0.10,  # 10-30% → CONCERNING
    # drift_critical_threshold: > 30% (相对) 或绝对差 > 0.5(数值)
    "drift_critical_threshold": 0.30,
    "drift_critical_absolute": 0.5,
    # 单调趋势告警:连续 N 次同向
    "monotonic_trend_min_consecutive": 3,
    # 历史保留
    "history_retention": 10000,
    # 基线快照自动设置(若无显式 baseline,第一次记录即为 baseline)
    "auto_baseline": True,
}


# =====================================================================
# Constitutional Drift Detector
# =====================================================================

class ConstitutionalDriftDetector:
    """宪法漂移检测器。

    用法:
        detector = get_constitutional_drift_detector()

        # 1. 设置基线(可选;若不设置,首次记录即基线)
        detector.set_baseline("confidence_threshold", 0.8)
        detector.set_baseline("safety_mode", "strict")

        # 2. 每次阈值变更记录
        detector.record_threshold(
            name="confidence_threshold",
            value=0.78,
            actor="ops-alice",
            reason=ChangeReason.MANUAL_TUNING,
            reason_text="减少误杀率(7 月反馈)",
        )

        # 3. 检测单次漂移
        alert = detector.check_drift("confidence_threshold")
        if alert and alert.severity == DriftSeverity.CRITICAL:
            # 触发 review
            ...

        # 4. 定期(每周)生成报告
        report = detector.get_drift_report()
        if report.has_critical_alerts:
            notify_sre(report)
    """

    def __init__(
        self,
        config: dict | None = None,
        store_path: str | None = None,
    ) -> None:
        self.config = {**DETECTOR_DEFAULTS, **(config or {})}
        self.store_path = store_path
        self._lock = threading.RLock()
        # 阈值历史(name -> list[ThresholdSnapshot])
        self._history: dict[str, list[ThresholdSnapshot]] = defaultdict(list)
        # 基线快照(name -> ThresholdSnapshot)
        self._baseline: dict[str, ThresholdSnapshot] = {}
        # 告警历史
        self._alerts: deque[DriftAlert] = deque(maxlen=self.config["history_retention"])
        # 统计
        self._stats: dict[str, int] = defaultdict(int)
        if store_path and os.path.exists(store_path):
            self._load()

    # ==================================================================
    # 基线管理
    # ==================================================================

    def set_baseline(
        self,
        name: str,
        value: Any,
        *,
        type: ThresholdType = ThresholdType.NUMERIC,
        actor: str = "system",
        tags: dict | None = None,
    ) -> ThresholdSnapshot:
        """设置阈值基线(用于后续漂移比对)。"""
        snap = ThresholdSnapshot(
            name=name,
            value=value,
            prev_value=None,  # 基线无 prev
            type=type,
            actor=actor,
            reason=ChangeReason.UNKNOWN,
            reason_text="baseline",
            tags=tags or {},
        )
        with self._lock:
            self._baseline[name] = snap
            # 也作为历史首条
            if name not in self._history:
                self._history[name].append(snap)
            self._save()
        logger.info("Baseline set: %s = %s", name, value)
        return snap

    def get_baseline(self, name: str) -> ThresholdSnapshot | None:
        with self._lock:
            return self._baseline.get(name)

    def reset_baseline(self, name: str) -> bool:
        """将当前值重置为基线(回滚操作)。"""
        with self._lock:
            current = self._get_current_snapshot(name)
            if current is None:
                return False
            baseline = self._baseline.get(name)
            if baseline is None:
                return False
            # 记录回滚
            rollback = ThresholdSnapshot(
                name=name,
                value=baseline.value,
                prev_value=current.value,
                type=baseline.type,
                actor="system",
                reason=ChangeReason.ROLLBACK,
                reason_text="Rollback to baseline (drift too large)",
                tags={"rolled_back_from": str(current.value)},
            )
            self._history[name].append(rollback)
            self._stats["rollbacks"] += 1
            self._save()
            logger.warning("Rolled back %s from %s to baseline %s", name, current.value, baseline.value)
            return True

    # ==================================================================
    # 阈值记录
    # ==================================================================

    def record_threshold(
        self,
        *,
        name: str,
        value: Any,
        actor: str = "system",
        reason: ChangeReason = ChangeReason.UNKNOWN,
        reason_text: str = "",
        type: ThresholdType | None = None,
        tags: dict | None = None,
    ) -> ThresholdSnapshot:
        """记录阈值变更。"""
        with self._lock:
            # 取上一值
            prev_value = None
            if self._history.get(name):
                prev_value = self._history[name][-1].value

            # 自动推断 type
            if type is None:
                type = self._infer_type(value, prev_value)

            snap = ThresholdSnapshot(
                name=name,
                value=value,
                prev_value=prev_value,
                type=type,
                actor=actor,
                reason=reason,
                reason_text=reason_text,
                tags=tags or {},
            )
            self._history[name].append(snap)

            # 若无基线且 auto_baseline,设为基线
            if name not in self._baseline and self.config["auto_baseline"]:
                self._baseline[name] = snap

            self._stats["records"] += 1
            self._save()

        logger.info(
            "Threshold recorded: %s = %s (prev=%s, actor=%s, reason=%s)",
            name, value, prev_value, actor, reason.value,
        )
        return snap

    def _infer_type(self, value: Any, prev_value: Any) -> ThresholdType:
        """自动推断阈值类型。"""
        if isinstance(value, bool) or isinstance(prev_value, bool):
            return ThresholdType.BOOLEAN
        if isinstance(value, (int, float)) or isinstance(prev_value, (int, float)):
            return ThresholdType.NUMERIC
        return ThresholdType.ENUM

    # ==================================================================
    # 漂移检测
    # ==================================================================

    def check_drift(self, name: str) -> DriftAlert | None:
        """检测单阈值的漂移。"""
        if not is_enabled("defense"):
            return None

        with self._lock:
            baseline = self._baseline.get(name)
            current = self._get_current_snapshot(name)

        if baseline is None or current is None:
            return None

        if baseline.value == current.value:
            return None  # 无漂移

        alert = self._compute_drift(name, baseline, current)

        with self._lock:
            self._alerts.append(alert)
            self._stats["drift_checks"] += 1
            if alert.severity == DriftSeverity.CRITICAL:
                self._stats["critical_alerts"] += 1
            elif alert.severity == DriftSeverity.CONCERNING:
                self._stats["concerning_alerts"] += 1

        return alert

    def _compute_drift(
        self,
        name: str,
        baseline: ThresholdSnapshot,
        current: ThresholdSnapshot,
    ) -> DriftAlert:
        """计算漂移量 + 严重度。"""
        alert = DriftAlert(
            threshold_name=name,
            current_value=current.value,
            baseline_value=baseline.value,
        )

        # 计算漂移量(按类型)
        if baseline.type == ThresholdType.NUMERIC:
            try:
                base_f = float(baseline.value)
                curr_f = float(current.value)
                alert.absolute_drift = abs(curr_f - base_f)
                if base_f != 0:
                    alert.relative_drift = alert.absolute_drift / abs(base_f)
                else:
                    alert.relative_drift = 1.0 if alert.absolute_drift > 0 else 0.0
                alert.direction = (
                    DriftDirection.UP if curr_f > base_f
                    else DriftDirection.DOWN if curr_f < base_f
                    else DriftDirection.NONE
                )
            except (TypeError, ValueError):
                alert.absolute_drift = 1.0
                alert.relative_drift = 1.0
                alert.direction = DriftDirection.OSCILLATING
        elif baseline.type == ThresholdType.ENUM:
            alert.absolute_drift = 1.0 if baseline.value != current.value else 0.0
            alert.relative_drift = 1.0 if baseline.value != current.value else 0.0
            alert.direction = DriftDirection.OSCILLATING
        else:  # BOOLEAN
            alert.absolute_drift = 1.0 if baseline.value != current.value else 0.0
            alert.relative_drift = 1.0 if baseline.value != current.value else 0.0
            alert.direction = DriftDirection.OSCILLATING

        # 严重度
        alert.severity = self._severity_for_drift(alert)

        # 单调趋势
        alert.consecutive_same_direction = self._consecutive_same_direction(name, alert.direction)
        if alert.consecutive_same_direction >= self.config["monotonic_trend_min_consecutive"]:
            # 升级严重度(单调趋势告警)
            if alert.severity == DriftSeverity.ACCEPTABLE:
                alert.severity = DriftSeverity.CONCERNING
                alert.message = (
                    f"Monotonic drift: {alert.consecutive_same_direction} consecutive "
                    f"{alert.direction.value} changes"
                )

        # 反制建议
        alert.countermeasure = self._suggest_countermeasure(alert)
        alert.message = alert.message or self._format_message(alert)
        return alert

    def _severity_for_drift(self, alert: DriftAlert) -> DriftSeverity:
        """根据漂移量计算严重度。"""
        rel = alert.relative_drift
        abs_d = alert.absolute_drift
        critical_rel = self.config["drift_critical_threshold"]
        critical_abs = self.config["drift_critical_absolute"]
        concerning = self.config["drift_concerning_threshold"]
        acceptable = self.config["drift_acceptable_threshold"]

        # 数值类型:相对 + 绝对取大
        if rel >= critical_rel or abs_d >= critical_abs:
            return DriftSeverity.CRITICAL
        if rel >= concerning:
            return DriftSeverity.CONCERNING
        if rel >= acceptable:
            return DriftSeverity.ACCEPTABLE
        return DriftSeverity.ACCEPTABLE  # 微小波动

    def _consecutive_same_direction(self, name: str, current_direction: DriftDirection) -> int:
        """计算连续同向变更次数。"""
        with self._lock:
            history = list(self._history.get(name, []))
        if len(history) < 2:
            return 0
        count = 0
        # 从后往前数(最近一次方向)
        for i in range(len(history) - 1, 0, -1):
            curr = history[i]
            prev = history[i - 1]
            if curr.value is None or prev.value is None:
                break
            # 计算这一步方向
            try:
                step_dir = (
                    DriftDirection.UP if float(curr.value) > float(prev.value)
                    else DriftDirection.DOWN if float(curr.value) < float(prev.value)
                    else DriftDirection.NONE
                )
            except (TypeError, ValueError):
                break
            if step_dir == current_direction and step_dir != DriftDirection.NONE:
                count += 1
            else:
                break
        return count

    @staticmethod
    def _format_message(alert: DriftAlert) -> str:
        return (
            f"Drift detected for {alert.threshold_name}: "
            f"baseline={alert.baseline_value}, current={alert.current_value}, "
            f"relative={alert.relative_drift:.1%}, absolute={alert.absolute_drift:.4f}, "
            f"direction={alert.direction.value}, severity={alert.severity.value}"
        )

    @staticmethod
    def _suggest_countermeasure(alert: DriftAlert) -> str:
        if alert.severity == DriftSeverity.CRITICAL:
            return "rollback_to_baseline"
        if alert.severity == DriftSeverity.CONCERNING:
            return "review_with_sre"
        return "monitor"

    # ==================================================================
    # 报告
    # ==================================================================

    def get_drift_report(self) -> DriftReport:
        """生成完整漂移报告。"""
        report = DriftReport()
        if not is_enabled("defense"):
            return report

        with self._lock:
            for name in self._history:
                alert = self.check_drift(name)
                if alert:
                    report.alerts.append(alert)
                # 当前值
                current = self._get_current_snapshot(name)
                if current:
                    report.current_thresholds[name] = current.value

            # 按 severity 排序
            severity_order = {
                DriftSeverity.CRITICAL: 0,
                DriftSeverity.CONCERNING: 1,
                DriftSeverity.ACCEPTABLE: 2,
                DriftSeverity.NONE: 3,
            }
            report.alerts.sort(key=lambda a: severity_order.get(a.severity, 4))

            # 取最早基线时间
            if self._baseline:
                report.baseline_set_at = min(s.timestamp for s in self._baseline.values())

            report.total_changes = sum(len(h) for h in self._history.values())
            report.has_critical_alerts = any(
                a.severity == DriftSeverity.CRITICAL for a in report.alerts
            )

        return report

    # ==================================================================
    # 查询 / 审计接口
    # ==================================================================

    def list_thresholds(self) -> dict[str, Any]:
        """列出所有当前阈值。"""
        with self._lock:
            result = {}
            for name in self._history:
                current = self._get_current_snapshot(name)
                if current:
                    result[name] = {
                        "current_value": current.value,
                        "type": current.type.value,
                        "last_changed_at": current.timestamp,
                        "last_actor": current.actor,
                    }
            return result

    def get_drift_history(self, name: str, limit: int = 100) -> list[ThresholdSnapshot]:
        """获取阈值变更历史。"""
        with self._lock:
            history = list(self._history.get(name, []))
        return history[-limit:]

    def get_alerts(
        self,
        *,
        severity: DriftSeverity | None = None,
        limit: int = 100,
    ) -> list[DriftAlert]:
        """获取告警列表。"""
        with self._lock:
            alerts = list(self._alerts)
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        return alerts[-limit:]

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
            stats["total_thresholds"] = len(self._history)
            stats["total_alerts"] = len(self._alerts)
            stats["baselines_set"] = len(self._baseline)
            return stats

    # ==================================================================
    # 内部
    # ==================================================================

    def _get_current_snapshot(self, name: str) -> ThresholdSnapshot | None:
        """获取当前(最新)快照。"""
        history = self._history.get(name, [])
        if not history:
            return None
        return history[-1]

    def _save(self) -> None:
        """持久化。"""
        if not self.store_path:
            return
        try:
            os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
            with self._lock:
                data = {
                    "history": {
                        name: [s.to_dict() for s in history]
                        for name, history in self._history.items()
                    },
                    "baseline": {
                        name: s.to_dict() for name, s in self._baseline.items()
                    },
                }
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save constitutional drift store: %s", e)

    def _load(self) -> None:
        if not self.store_path or not os.path.exists(self.store_path):
            return
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for name, snapshots_data in data.get("history", {}).items():
                    for s_data in snapshots_data:
                        s_data["type"] = ThresholdType(s_data["type"])
                        s_data["reason"] = ChangeReason(s_data["reason"])
                        snap = ThresholdSnapshot(**s_data)
                        self._history[name].append(snap)
                for name, s_data in data.get("baseline", {}).items():
                    s_data["type"] = ThresholdType(s_data["type"])
                    s_data["reason"] = ChangeReason(s_data["reason"])
                    self._baseline[name] = ThresholdSnapshot(**s_data)
            logger.info("Loaded constitutional drift store from %s", self.store_path)
        except Exception as e:
            logger.error("Failed to load constitutional drift store: %s", e)


# =====================================================================
# 全局单例
# =====================================================================

_detector_instance: ConstitutionalDriftDetector | None = None
_detector_lock = threading.RLock()


def get_constitutional_drift_detector() -> ConstitutionalDriftDetector:
    """获取全局 ConstitutionalDriftDetector 单例。"""
    global _detector_instance
    with _detector_lock:
        if _detector_instance is None:
            _detector_instance = ConstitutionalDriftDetector()
        return _detector_instance


def reset_constitutional_drift_detector() -> None:
    """重置全局单例(测试用)。"""
    global _detector_instance
    with _detector_lock:
        _detector_instance = None


__all__ = [
    "ChangeReason",
    "ConstitutionalDriftDetector",
    "DriftAlert",
    "DriftDirection",
    "DriftReport",
    "DriftSeverity",
    "ThresholdSnapshot",
    "ThresholdType",
    "get_constitutional_drift_detector",
    "reset_constitutional_drift_detector",
]
