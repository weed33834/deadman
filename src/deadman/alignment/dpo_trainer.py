"""P8.7 Direct Preference Optimization (DPO) 训练框架。

参考论文: "Direct Preference Optimization: Your Language Model is Secretly a Reward Model"
    (Rafailov et al., 2023)

DPO 核心思想:
    - 不需显式 reward model,直接用偏好对 (chosen, rejected) 优化策略
    - 损失函数: L_DPO = -log σ(β · (log π_θ(y_w|x) / π_ref(y_w|x)
                                    - log π_θ(y_l|x) / π_ref(y_l|x)))

本模块特点(生产级约束):
    - NO actual LLM training:仅模拟训练流程,产出 mock 指标(用于 CI 与流程验证)
    - PII redaction:所有偏好对入库前过 PIIRedactor
    - Trust score filter:基于用户历史信任度过滤低质量偏好(防止恶意 / 噪声样本污染)
    - Atomic JSONL persistence:.tmp + os.replace 原子写,threading.RLock 并发安全
    - Tenant isolation:数据路径按 tenant_id 分目录
    - Feature flag:`DEADMAN_ALIGNMENT_ENABLED=0` 默认关闭

依赖:
    - 标准库 + deadman.infrastructure.{feature_flags, multi_tenant, defense.pii_guard}
    - 不依赖 torch / transformers / trl
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.defense.pii_guard import PIIRedactor, get_pii_redactor
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path

logger = logging.getLogger(__name__)


# =====================================================================
# 偏好来源 - 标识样本来源,影响 trust score 计算
# =====================================================================
class PreferenceSource(str, Enum):
    """偏好样本来源。

    - USER_FEEDBACK: 用户显式反馈(评分 / 选择),高可信
    - AUTO_EVAL: LLM-as-Judge 自动评估,中可信
    - REFLEXION: Reflexion 反思提取的偏好,中可信
    - SYNTHETIC: 合成数据(模板 / 增强),低可信
    """

    USER_FEEDBACK = "user_feedback"
    AUTO_EVAL = "auto_eval"
    REFLEXION = "reflexion"
    SYNTHETIC = "synthetic"


# 默认各来源的信任权重
_SOURCE_TRUST_WEIGHT: dict[PreferenceSource, float] = {
    PreferenceSource.USER_FEEDBACK: 1.0,
    PreferenceSource.AUTO_EVAL: 0.6,
    PreferenceSource.REFLEXION: 0.5,
    PreferenceSource.SYNTHETIC: 0.2,
}


# =====================================================================
# 数据类
# =====================================================================
@dataclass
class DPOConfig:
    """DPO 训练超参。

    Attributes:
        beta: KL 散度正则强度(论文 β),典型 0.1-0.5
        learning_rate: 学习率(仅用于 mock 指标展示)
        batch_size: 批大小
        max_steps: 最大训练步数
        save_steps: 每 N 步保存 checkpoint
        min_trust_score: 偏好样本最低信任分(低于此值过滤)
        redact_pii: 是否对样本做 PII 脱敏(默认 True)
    """

    beta: float = 0.1
    learning_rate: float = 5e-7
    batch_size: int = 8
    max_steps: int = 100
    save_steps: int = 20
    min_trust_score: float = 0.3
    redact_pii: bool = True


@dataclass
class PreferenceExample:
    """单条偏好样本(chosen > rejected)。

    Attributes:
        prompt: 共享 prompt
        chosen_response: 偏好(更好)回复
        rejected_response: 拒绝(更差)回复
        source: 来源(影响 trust score)
        trust_score: 信任分(0-1),低于阈值时过滤
        timestamp: 收集时间(epoch)
        user_id: 提供者(用于追溯 / 删除)
        redacted: 是否已 PII 脱敏
    """

    prompt: str
    chosen_response: str
    rejected_response: str
    source: PreferenceSource = PreferenceSource.USER_FEEDBACK
    trust_score: float = 1.0
    timestamp: float = field(default_factory=time.time)
    user_id: str = ""
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source"] = self.source.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreferenceExample:
        # 兼容 source 字段(string / enum)
        source = data.get("source", PreferenceSource.USER_FEEDBACK.value)
        if isinstance(source, str):
            try:
                source = PreferenceSource(source)
            except ValueError:
                source = PreferenceSource.USER_FEEDBACK
        return cls(
            prompt=data["prompt"],
            chosen_response=data["chosen_response"],
            rejected_response=data["rejected_response"],
            source=source,
            trust_score=float(data.get("trust_score", 1.0)),
            timestamp=float(data.get("timestamp", time.time())),
            user_id=data.get("user_id", ""),
            redacted=bool(data.get("redacted", False)),
        )


@dataclass
class TrainingReport:
    """训练报告(mock 指标 + 实际样本统计)。"""

    total_steps: int = 0
    samples_used: int = 0
    samples_filtered: int = 0
    final_loss: float = 0.0
    final_reward_accuracy: float = 0.0  # chosen > rejected 比例
    final_kl_divergence: float = 0.0
    checkpoints_saved: int = 0
    duration_seconds: float = 0.0
    completed: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalReport:
    """评估报告。

    Attributes:
        accuracy: chosen > rejected 的准确率
        margin_mean: chosen_logp - rejected_logp 的均值(mock)
        samples_evaluated: 评估样本数
        per_source_accuracy: 按来源分组的准确率
    """

    accuracy: float = 0.0
    margin_mean: float = 0.0
    samples_evaluated: int = 0
    per_source_accuracy: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =====================================================================
# Trust Score Tracker - 基于历史的低质量偏好过滤
# =====================================================================
class TrustScoreTracker:
    """用户 / 来源级信任分跟踪器。

    基于 defense 模块的设计原则:不信任任何单条样本,通过历史聚合计算可信度。

    信任分计算:
        user_trust(user_id) = (positive_count - negative_count) / (total + 1)
        source_trust(source) = _SOURCE_TRUST_WEIGHT[source]
        combined = 0.6 * user_trust + 0.4 * source_trust

    负反馈事件(用户标记"不满意"或样本被评估为低质量)会拉低该 user 的信任分。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # user_id → {positive: int, negative: int}
        self._user_history: dict[str, dict[str, int]] = {}

    def record_outcome(self, user_id: str, positive: bool) -> None:
        """记录一次反馈结果(positive = 高质量 / accepted)。"""
        if not user_id:
            return
        with self._lock:
            hist = self._user_history.setdefault(user_id, {"positive": 0, "negative": 0})
            if positive:
                hist["positive"] += 1
            else:
                hist["negative"] += 1

    def user_trust(self, user_id: str) -> float:
        """单用户信任分(0-1)。无历史 → 0.5(中性)。"""
        if not user_id:
            return 0.5
        with self._lock:
            hist = self._user_history.get(user_id)
            if not hist or (hist["positive"] + hist["negative"]) == 0:
                return 0.5
            total = hist["positive"] + hist["negative"]
            # (pos - neg) / (total + 1) → 范围约 (-1, 1),归一到 (0, 1)
            raw = (hist["positive"] - hist["negative"]) / (total + 1)
            return max(0.0, min(1.0, 0.5 + raw / 2))

    def source_trust(self, source: PreferenceSource) -> float:
        """来源权重(静态查表)。"""
        return _SOURCE_TRUST_WEIGHT.get(source, 0.5)

    def combined_trust(self, user_id: str, source: PreferenceSource) -> float:
        """综合信任分 = 0.6 * user + 0.4 * source。"""
        return 0.6 * self.user_trust(user_id) + 0.4 * self.source_trust(source)

    def reset(self) -> None:
        with self._lock:
            self._user_history.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "user_count": len(self._user_history),
                "users": dict(self._user_history),
            }


# =====================================================================
# DPOTrainer
# =====================================================================
class DPOTrainer:
    """Direct Preference Optimization 训练器。

    用法:
        trainer = DPOTrainer()
        trainer.add_preference(PreferenceExample(...))
        report = trainer.train(DPOConfig(max_steps=50))
        trainer.save_checkpoint(path)

    线程安全:所有公共方法加 RLock。
    持久化:JSONL 文件,原子写(.tmp + os.replace)。
    """

    def __init__(
        self,
        pii_redactor: PIIRedactor | None = None,
        trust_tracker: TrustScoreTracker | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._preferences: list[PreferenceExample] = []
        self._pii_redactor = pii_redactor or get_pii_redactor()
        self._trust = trust_tracker or TrustScoreTracker()
        # 训练状态(checkpoint load 后填充)
        self._last_checkpoint: dict[str, Any] = {}
        self._trained: bool = False

    # ------------------------------------------------------------------
    # 偏好收集
    # ------------------------------------------------------------------
    def add_preference(self, example: PreferenceExample) -> bool:
        """添加一条偏好样本。

        流程:
            1. 计算 trust_score(若未设置)
            2. PII 脱敏(prompt / chosen / rejected)
            3. 低于 min_trust_score 的样本被拒收(默认 0.3,见 DPOConfig)

        Returns:
            True 接收 / False 拒收(低质量)
        """
        with self._lock:
            # 1. 计算 trust(若 caller 未设置)
            if example.trust_score <= 0:
                example.trust_score = self._trust.combined_trust(example.user_id, example.source)

            # 2. PII 脱敏(默认开启)
            if not example.redacted:
                example.prompt = self._pii_redactor.redact(example.prompt).redacted_text
                example.chosen_response = self._pii_redactor.redact(
                    example.chosen_response
                ).redacted_text
                example.rejected_response = self._pii_redactor.redact(
                    example.rejected_response
                ).redacted_text
                example.redacted = True

            # 3. 默认最低信任分阈值(此处用静态默认,train 时可再用 config 过滤)
            if example.trust_score < 0.1:
                logger.warning(
                    "DPO preference rejected: trust_score=%.3f user=%s source=%s",
                    example.trust_score,
                    example.user_id,
                    example.source.value,
                )
                self._trust.record_outcome(example.user_id, positive=False)
                return False

            self._preferences.append(example)
            self._trust.record_outcome(example.user_id, positive=True)
            return True

    def preferences(self) -> list[PreferenceExample]:
        """返回当前偏好样本快照(拷贝)。"""
        with self._lock:
            return list(self._preferences)

    def preference_count(self) -> int:
        with self._lock:
            return len(self._preferences)

    def clear(self) -> None:
        with self._lock:
            self._preferences.clear()
            self._trained = False
            self._last_checkpoint = {}

    # ------------------------------------------------------------------
    # 训练(模拟)
    # ------------------------------------------------------------------
    def train(self, config: DPOConfig) -> TrainingReport:
        """运行 DPO 训练(mock,不调 LLM / GPU)。

        模拟指标:
            - loss: 从 2.2 指数衰减到 ~0.3
            - reward_accuracy: 从 0.5 上升到 ~0.85
            - kl_divergence: 从 0 上升到 β · 5
        """
        start_ts = time.time()
        report = TrainingReport()

        with self._lock:
            # 1. 按 min_trust_score 过滤
            kept = [p for p in self._preferences if p.trust_score >= config.min_trust_score]
            report.samples_used = len(kept)
            report.samples_filtered = len(self._preferences) - len(kept)

            if not kept:
                report.completed = False
                report.error = "no samples after trust filter"
                report.duration_seconds = time.time() - start_ts
                logger.warning("DPO train aborted: %s", report.error)
                return report

            # 2. mock 训练循环
            steps = max(1, min(config.max_steps, 1000))
            checkpoints_saved = 0
            for step in range(1, steps + 1):
                # 指数衰减 loss
                decay = math.exp(-step / max(1, steps / 4))
                report.final_loss = 0.3 + 1.9 * decay
                # reward accuracy 单调上升
                report.final_reward_accuracy = 0.5 + 0.35 * (1 - decay)
                # KL 散度
                report.final_kl_divergence = config.beta * 5 * (1 - decay)
                # checkpoint
                if config.save_steps > 0 and step % config.save_steps == 0:
                    checkpoints_saved += 1
                    self._last_checkpoint = {
                        "step": step,
                        "loss": report.final_loss,
                        "reward_accuracy": report.final_reward_accuracy,
                        "config": asdict(config),
                    }

            report.total_steps = steps
            report.checkpoints_saved = checkpoints_saved
            report.completed = True
            report.duration_seconds = time.time() - start_ts
            self._trained = True
            logger.info(
                "DPO train done: steps=%d loss=%.3f acc=%.3f kl=%.3f",
                steps,
                report.final_loss,
                report.final_reward_accuracy,
                report.final_kl_divergence,
            )
            return report

    # ------------------------------------------------------------------
    # Checkpoint 持久化
    # ------------------------------------------------------------------
    def save_checkpoint(self, path: str | Path) -> Path:
        """保存 checkpoint 到 JSON 文件(原子写)。

        Returns:
            实际写入的 Path
        """
        path = Path(path)
        with self._lock:
            data = {
                "version": 1,
                "saved_at": time.time(),
                "tenant_id": get_current_tenant_id(),
                "preferences_count": len(self._preferences),
                "trained": self._trained,
                "last_checkpoint": self._last_checkpoint,
                "trust_snapshot": self._trust.snapshot(),
                # 同时落盘偏好样本(便于跨进程恢复)
                "preferences": [p.to_dict() for p in self._preferences],
            }
        return _atomic_write_json(path, data)

    def load_checkpoint(self, path: str | Path) -> bool:
        """从 JSON 文件加载 checkpoint。

        Returns:
            True 成功 / False 失败
        """
        path = Path(path)
        if not path.exists():
            logger.warning("DPO checkpoint not found: %s", path)
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("DPO checkpoint load failed: %s", e)
            return False

        with self._lock:
            self._preferences.clear()
            for p_data in data.get("preferences", []):
                try:
                    self._preferences.append(PreferenceExample.from_dict(p_data))
                except (KeyError, ValueError) as e:
                    logger.warning("Skip malformed preference: %s", e)
            self._trained = bool(data.get("trained", False))
            self._last_checkpoint = data.get("last_checkpoint", {}) or {}
            # 恢复 trust 历史(尽力)
            trust_snap = data.get("trust_snapshot") or {}
            for user_id, hist in (trust_snap.get("users") or {}).items():
                pos = int(hist.get("positive", 0))
                neg = int(hist.get("negative", 0))
                for _ in range(pos):
                    self._trust.record_outcome(user_id, positive=True)
                for _ in range(neg):
                    self._trust.record_outcome(user_id, positive=False)
        logger.info(
            "DPO checkpoint loaded: %s (%d preferences)",
            path,
            len(self._preferences),
        )
        return True

    # ------------------------------------------------------------------
    # 评估
    # ------------------------------------------------------------------
    def evaluate(self, eval_set: list[PreferenceExample]) -> EvalReport:
        """在评估集上计算 mock 指标。

        真实 DPO 评估会用 trained policy 计算 logp(chosen) - logp(rejected),
        此处简化为:用 response 长度 / 信任分作为 mock 信号。
        """
        report = EvalReport()
        if not eval_set:
            return report

        correct = 0
        margins: list[float] = []
        per_source_correct: dict[str, int] = {}
        per_source_total: dict[str, int] = {}

        for ex in eval_set:
            # mock: chosen 通常更长 + trust 更高 → margin > 0
            chosen_signal = len(ex.chosen_response) / 100.0 + ex.trust_score
            rejected_signal = len(ex.rejected_response) / 100.0 + (1 - ex.trust_score)
            margin = chosen_signal - rejected_signal
            margins.append(margin)
            if margin > 0:
                correct += 1
                per_source_correct[ex.source.value] = per_source_correct.get(ex.source.value, 0) + 1
            per_source_total[ex.source.value] = per_source_total.get(ex.source.value, 0) + 1

        report.samples_evaluated = len(eval_set)
        report.accuracy = correct / len(eval_set)
        report.margin_mean = sum(margins) / len(margins) if margins else 0.0
        report.per_source_accuracy = {
            src: per_source_correct[src] / per_source_total[src]
            for src in per_source_total
            if per_source_total[src] > 0
        }
        return report

    # ------------------------------------------------------------------
    # 内部 - 持久化到 tenant 数据目录(可选,供 AlignmentManager 调用)
    # ------------------------------------------------------------------
    def persist_to_tenant(self, filename: str = "alignment/dpo_preferences.jsonl") -> Path:
        """原子追加偏好样本到 tenant 数据目录(JSONL)。"""
        target = resolve_data_path(filename)
        with self._lock:
            lines = [json.dumps(p.to_dict(), ensure_ascii=False) for p in self._preferences]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        return target

    @property
    def trust_tracker(self) -> TrustScoreTracker:
        return self._trust


# =====================================================================
# 原子写 JSON
# =====================================================================
def _atomic_write_json(path: Path, data: dict[str, Any]) -> Path:
    """原子写 JSON(.tmp + os.replace),自动建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path
