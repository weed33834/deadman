"""P8.7 持续学习模块 - 从用户反馈中学习。

功能:
    - 收集用户反馈(评分 + 评论)
    - 自动提取 DPO 偏好对(rating ≥ 4 → chosen, rating < 3 → rejected)
    - 周报:聚合统计 + 标记低质反馈待人工 review
    - 自动晋升高质量样本到 SFT 数据集
    - GDPR 被遗忘权(forget_user)

与 Reflexion 模块集成(可选):
    - 显式反馈(用户评分)+ 隐式 reflexion 信号(失败重试模式)合并
    - 提取 reflexion 失败 → rejected pair

不依赖 torch / transformers。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.defense.pii_guard import PIIRedactor, get_pii_redactor
from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path
from .dpo_trainer import PreferenceExample, PreferenceSource
from .sft_dataset import SFTDataset, SFTExample, SFTSource, TaskType

logger = logging.getLogger(__name__)


# Reflexion 集成(可选,模块缺失则降级)
try:
    from ..reflexion import ReflexionEngine  # type: ignore
    _HAS_REFLEXION = True
except ImportError:  # pragma: no cover - reflexion 缺失场景
    ReflexionEngine = None  # type: ignore
    _HAS_REFLEXION = False


# 评分阈值
RATING_CHOSEN_THRESHOLD = 4   # ≥ 4 → chosen
RATING_REJECTED_THRESHOLD = 3  # < 3 → rejected


# =====================================================================
# 数据类
# =====================================================================
@dataclass
class FeedbackEvent:
    """用户反馈事件。

    Attributes:
        user_id: 用户 ID
        query: 用户原始查询
        response: 系统回复
        rating: 评分(1-5)
        comment: 评论(可选)
        timestamp: 时间戳
        task_type: 任务类型(可选)
        conversation_id: 会话 ID(可选)
    """

    user_id: str
    query: str
    response: str
    rating: int
    comment: str = ""
    timestamp: float = field(default_factory=time.time)
    task_type: str = ""
    conversation_id: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.rating <= 5:
            raise ValueError(f"rating must be in [1, 5], got {self.rating}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedbackEvent":
        return cls(
            user_id=data["user_id"],
            query=data["query"],
            response=data["response"],
            rating=int(data["rating"]),
            comment=data.get("comment", ""),
            timestamp=float(data.get("timestamp", time.time())),
            task_type=data.get("task_type", ""),
            conversation_id=data.get("conversation_id", ""),
        )


@dataclass
class WeeklyReport:
    """周报。

    Attributes:
        period_start: 周期起始(epoch)
        period_end: 周期结束(epoch)
        total_feedback: 总反馈数
        avg_rating: 平均评分
        rating_distribution: {1: count, 2: count, ..., 5: count}
        preference_pairs_extracted: 提取的偏好对数
        users_active: 活跃用户数
        flagged_for_review: 标记待 review 的反馈数
        task_distribution: 任务类型分布
        top_comments: 最常用评论(取前 5)
    """

    period_start: float = 0.0
    period_end: float = 0.0
    total_feedback: int = 0
    avg_rating: float = 0.0
    rating_distribution: dict[int, int] = field(default_factory=dict)
    preference_pairs_extracted: int = 0
    users_active: int = 0
    flagged_for_review: int = 0
    task_distribution: dict[str, int] = field(default_factory=dict)
    top_comments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =====================================================================
# ContinuousLearner
# =====================================================================
class ContinuousLearner:
    """持续学习引擎。

    用法:
        learner = ContinuousLearner()
        event = FeedbackEvent(user_id="u1", query="...", response="...", rating=5)
        learner.record_feedback(event)
        pref = learner.extract_preference_pair(event)
        report = learner.weekly_review()
        learner.auto_promote_to_sft(sft_dataset, min_quality_score=0.8)
        learner.forget_user("u1")  # GDPR
    """

    def __init__(
        self,
        pii_redactor: Optional[PIIRedactor] = None,
        reflexion_engine: Optional[Any] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._events: list[FeedbackEvent] = []
        self._pii_redactor = pii_redactor or get_pii_redactor()
        # Reflexion 集成(可选)
        self._reflexion = reflexion_engine
        if self._reflexion is None and _HAS_REFLEXION:
            try:
                # 不实际实例化(需要 LLM),仅占位,供测试注入
                self._reflexion = None
            except Exception:
                self._reflexion = None

    # ------------------------------------------------------------------
    # 反馈收集
    # ------------------------------------------------------------------
    def record_feedback(self, event: FeedbackEvent) -> bool:
        """记录反馈事件(PII 脱敏后存储)。

        Returns:
            True 成功
        """
        with self._lock:
            # PII 脱敏(query + response)
            event.query = self._pii_redactor.redact(event.query).redacted_text
            event.response = self._pii_redactor.redact(event.response).redacted_text
            if event.comment:
                event.comment = self._pii_redactor.redact(event.comment).redacted_text
            self._events.append(event)
            return True

    def events(self) -> list[FeedbackEvent]:
        with self._lock:
            return list(self._events)

    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    # ------------------------------------------------------------------
    # 偏好对提取
    # ------------------------------------------------------------------
    def extract_preference_pair(
        self, event: FeedbackEvent
    ) -> Optional[PreferenceExample]:
        """从反馈事件提取偏好对。

        规则:
            - rating ≥ 4 → chosen = response(高赞回复)
            - rating < 3 → rejected = response(差评回复)
            - rating = 3 → 中性,返回 None
            - chosen/rejected 的对端用历史平均回复或空字符串(mock)

        若 Reflexion 可用,可结合失败重试模式生成更强对比:
            - reflexion 失败的 response → rejected
            - reflexion 成功的 response → chosen
        """
        if RATING_REJECTED_THRESHOLD <= event.rating < RATING_CHOSEN_THRESHOLD:
            # 中性,不构成偏好
            return None

        if event.rating >= RATING_CHOSEN_THRESHOLD:
            # 高赞 → chosen
            return PreferenceExample(
                prompt=event.query,
                chosen_response=event.response,
                rejected_response="",  # 无显式对端,留给后续匹配
                source=PreferenceSource.USER_FEEDBACK,
                trust_score=1.0,
                user_id=event.user_id,
                redacted=True,
            )

        if event.rating < RATING_REJECTED_THRESHOLD:
            # 差评 → rejected
            return PreferenceExample(
                prompt=event.query,
                chosen_response="",  # 无显式对端
                rejected_response=event.response,
                source=PreferenceSource.USER_FEEDBACK,
                trust_score=0.5,  # 差评信任分较低
                user_id=event.user_id,
                redacted=True,
            )

        return None

    def extract_all_preference_pairs(self) -> list[PreferenceExample]:
        """从所有已记录反馈中批量提取偏好对。"""
        with self._lock:
            events = list(self._events)
        pairs: list[PreferenceExample] = []
        for event in events:
            pair = self.extract_preference_pair(event)
            if pair is not None:
                pairs.append(pair)

        # 尝试匹配 chosen/rejected 对端(同 prompt 的正负反馈配对)
        pairs = self._match_complementary_pairs(pairs)
        return pairs

    def _match_complementary_pairs(
        self, pairs: list[PreferenceExample]
    ) -> list[PreferenceExample]:
        """将同 prompt 的 chosen-only / rejected-only 配对为完整 pair。

        匹配规则:
            - 找同 prompt 的 chosen-only 和 rejected-only
            - 合并为完整 pair(chosen + rejected)
        """
        chosen_only: list[PreferenceExample] = []
        rejected_only: list[PreferenceExample] = []
        complete: list[PreferenceExample] = []
        for p in pairs:
            if p.chosen_response and p.rejected_response:
                complete.append(p)
            elif p.chosen_response:
                chosen_only.append(p)
            elif p.rejected_response:
                rejected_only.append(p)

        # 按 prompt 分组
        chosen_by_prompt: dict[str, PreferenceExample] = {
            p.prompt: p for p in chosen_only
        }
        rejected_by_prompt: dict[str, PreferenceExample] = {
            p.prompt: p for p in rejected_only
        }

        matched: list[PreferenceExample] = []
        consumed_chosen: set[str] = set()
        consumed_rejected: set[str] = set()
        for prompt, ch in chosen_by_prompt.items():
            if prompt in rejected_by_prompt:
                rj = rejected_by_prompt[prompt]
                matched.append(PreferenceExample(
                    prompt=prompt,
                    chosen_response=ch.chosen_response,
                    rejected_response=rj.rejected_response,
                    source=PreferenceSource.USER_FEEDBACK,
                    trust_score=0.8,
                    user_id=ch.user_id or rj.user_id,
                    redacted=True,
                ))
                consumed_chosen.add(prompt)
                consumed_rejected.add(prompt)

        # 未配对的保留(对端为空)
        for prompt, ch in chosen_by_prompt.items():
            if prompt not in consumed_chosen:
                matched.append(ch)
        for prompt, rj in rejected_by_prompt.items():
            if prompt not in consumed_rejected:
                matched.append(rj)

        return complete + matched

    # ------------------------------------------------------------------
    # 周报
    # ------------------------------------------------------------------
    def weekly_review(self, days: int = 7) -> WeeklyReport:
        """生成最近 N 天的周报。"""
        now = time.time()
        period_start = now - days * 86400
        with self._lock:
            recent = [e for e in self._events if e.timestamp >= period_start]

        if not recent:
            return WeeklyReport(
                period_start=period_start, period_end=now,
                total_feedback=0, avg_rating=0.0,
            )

        rating_sum = 0
        rating_dist: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        users: set[str] = set()
        task_dist: dict[str, int] = {}
        flagged = 0
        comments: list[str] = []

        for e in recent:
            rating_sum += e.rating
            rating_dist[e.rating] = rating_dist.get(e.rating, 0) + 1
            users.add(e.user_id)
            if e.task_type:
                task_dist[e.task_type] = task_dist.get(e.task_type, 0) + 1
            # 低分 + 有评论 → 标记 review
            if e.rating <= 2 and e.comment:
                flagged += 1
            if e.comment:
                comments.append(e.comment)

        # 提取偏好对数
        preference_pairs = sum(
            1 for e in recent
            if e.rating >= RATING_CHOSEN_THRESHOLD or e.rating < RATING_REJECTED_THRESHOLD
        )

        # 取前 5 条评论(去重)
        seen = set()
        top_comments: list[str] = []
        for c in comments:
            if c not in seen:
                seen.add(c)
                top_comments.append(c)
            if len(top_comments) >= 5:
                break

        return WeeklyReport(
            period_start=period_start,
            period_end=now,
            total_feedback=len(recent),
            avg_rating=rating_sum / len(recent),
            rating_distribution=rating_dist,
            preference_pairs_extracted=preference_pairs,
            users_active=len(users),
            flagged_for_review=flagged,
            task_distribution=task_dist,
            top_comments=top_comments,
        )

    # ------------------------------------------------------------------
    # 自动晋升到 SFT
    # ------------------------------------------------------------------
    def auto_promote_to_sft(
        self,
        sft_dataset: SFTDataset,
        min_quality_score: float = 0.8,
        min_rating: int = 4,
    ) -> int:
        """自动将高赞反馈晋升到 SFT 数据集。

        条件:
            - rating ≥ min_rating(默认 4)
            - 计算质量分 = rating / 5 + (有评论 ? 0.1 : 0)
            - 质量分 ≥ min_quality_score(默认 0.8)

        Returns:
            晋升条数
        """
        promoted = 0
        with self._lock:
            events = list(self._events)

        for event in events:
            if event.rating < min_rating:
                continue
            # 质量分 = rating / 5 + (有评论 ? 0.1 : 0)
            quality = event.rating / 5.0 + (0.1 if event.comment else 0.0)
            quality = min(1.0, quality)
            if quality < min_quality_score:
                continue

            # 任务类型映射
            task_type = TaskType.GENERAL
            if event.task_type:
                try:
                    task_type = TaskType(event.task_type)
                except ValueError:
                    task_type = TaskType.GENERAL

            sft_example = SFTExample(
                prompt=event.query,
                completion=event.response,
                task_type=task_type,
                quality_score=quality,
                source=SFTSource.USER_FEEDBACK,
                user_id=event.user_id,
                redacted=True,
            )
            sft_dataset.add(sft_example)
            promoted += 1

        logger.info("Auto-promoted %d feedback events to SFT", promoted)
        return promoted

    # ------------------------------------------------------------------
    # GDPR 被遗忘权
    # ------------------------------------------------------------------
    def forget_user(self, user_id: str) -> int:
        """删除某用户的所有反馈记录(GDPR Right to Erasure)。

        Returns:
            删除条数
        """
        with self._lock:
            before = len(self._events)
            self._events = [e for e in self._events if e.user_id != user_id]
            removed = before - len(self._events)
        logger.info(
            "forget_user(%s): removed %d feedback events", user_id, removed
        )
        return removed

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def save(self, filename: str = "alignment/feedback_events.jsonl") -> Path:
        """原子写入 tenant 数据目录(JSONL)。"""
        target = resolve_data_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with self._lock:
            events = list(self._events)
        with open(tmp, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        return target

    def load(self, filename: str = "alignment/feedback_events.jsonl") -> int:
        """从 tenant 数据目录加载。"""
        target = resolve_data_path(filename)
        if not target.exists():
            return 0
        loaded = 0
        with self._lock:
            self._events.clear()
            with open(target, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        self._events.append(FeedbackEvent.from_dict(data))
                        loaded += 1
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.warning("Skip malformed feedback line: %s", e)
        return loaded

    # ------------------------------------------------------------------
    # Reflexion 集成
    # ------------------------------------------------------------------
    def inject_reflexion_signal(
        self,
        query: str,
        failed_response: str,
        recovered_response: str,
        user_id: str = "",
    ) -> Optional[PreferenceExample]:
        """注入 Reflexion 反思信号(失败重试 → 偏好对)。

        Reflexion 在调用失败后通过「反思-调整-重试」恢复,
        其中"失败的 response"是 rejected,"重试成功的 response"是 chosen,
        形成天然的偏好对。

        Args:
            query: 原始查询
            failed_response: 失败的回复(被反思掉的)
            recovered_response: 重试成功的回复
            user_id: 用户 ID

        Returns:
            PreferenceExample 或 None(若 Reflexion 不可用)
        """
        if not _HAS_REFLEXION:
            logger.debug("Reflexion module unavailable, skip signal injection")
            return None

        if not failed_response or not recovered_response:
            return None

        # PII 脱敏
        query = self._pii_redactor.redact(query).redacted_text
        failed_response = self._pii_redactor.redact(failed_response).redacted_text
        recovered_response = self._pii_redactor.redact(recovered_response).redacted_text

        return PreferenceExample(
            prompt=query,
            chosen_response=recovered_response,
            rejected_response=failed_response,
            source=PreferenceSource.REFLEXION,
            trust_score=0.7,
            user_id=user_id,
            redacted=True,
        )

    @property
    def has_reflexion(self) -> bool:
        return _HAS_REFLEXION
