"""P8.3.4 知识源信任度评分。

设计目标:
    - 不同来源的知识按 "可信度" 评分(0.0-1.0),用于 fusion 时加权
    - 7 个预设等级:OFFICIAL_LAW / COURT_CASE / GOVERNMENT_DOC / LAWYER_VERIFIED /
      USER_EXPERIENCE / AI_GENERATED / UNVERIFIED
    - 支持基于用户反馈调整(score update with delta)
    - 文件持久化(source → 当前分数 + 历史)
    - aggregate 加权聚合:低置信度评分(偏离群体)权重更高,体现"少数意见不被淹没"

法规依据:
    - 《生成式 AI 管理办法》第 9 条:训练数据来源合法、标注真实
    - PIPL 第 8 条:个人信息处理应保证质量,避免错误

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 原子写:持久化用 .tmp + os.replace
    - 线程安全:threading.RLock
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.feature_flags import is_enabled

logger = logging.getLogger(__name__)


class TrustLevel(str, Enum):
    """知识源信任度等级(预设默认分数)。

    按可信度从高到低:
        - OFFICIAL_LAW: 官方法律法规(如《民法典》)→ 0.95
        - GOVERNMENT_DOC: 政府文件(部委规章)→ 0.90
        - COURT_CASE: 法院判例 → 0.85
        - LAWYER_VERIFIED: 律师审核内容 → 0.80
        - USER_EXPERIENCE: 用户实操经验 → 0.50
        - AI_GENERATED: AI 生成内容 → 0.40
        - UNVERIFIED: 未验证来源 → 0.20
    """

    OFFICIAL_LAW = "official_law"            # 官方法律法规
    GOVERNMENT_DOC = "government_doc"        # 政府文件(部委规章)
    COURT_CASE = "court_case"                # 法院判例
    LAWYER_VERIFIED = "lawyer_verified"      # 律师审核
    USER_EXPERIENCE = "user_experience"      # 用户实操经验
    AI_GENERATED = "ai_generated"            # AI 生成
    UNVERIFIED = "unverified"                 # 未验证


# 预设默认信任分数(0.0-1.0)
_DEFAULT_TRUST_SCORES: dict[TrustLevel, float] = {
    TrustLevel.OFFICIAL_LAW: 0.95,
    TrustLevel.GOVERNMENT_DOC: 0.90,
    TrustLevel.COURT_CASE: 0.85,
    TrustLevel.LAWYER_VERIFIED: 0.80,
    TrustLevel.USER_EXPERIENCE: 0.50,
    TrustLevel.AI_GENERATED: 0.40,
    TrustLevel.UNVERIFIED: 0.20,
}


# 来源前缀匹配规则:source 字符串前缀 → TrustLevel
# 例:"official_law:cn" → OFFICIAL_LAW,"court_case:bj-2024-001" → COURT_CASE
_SOURCE_PREFIX_MAP: list[tuple[str, TrustLevel]] = [
    ("official_law:", TrustLevel.OFFICIAL_LAW),
    ("government_doc:", TrustLevel.GOVERNMENT_DOC),
    ("court_case:", TrustLevel.COURT_CASE),
    ("lawyer_verified:", TrustLevel.LAWYER_VERIFIED),
    ("user_experience:", TrustLevel.USER_EXPERIENCE),
    ("ai_generated:", TrustLevel.AI_GENERATED),
    ("unverified:", TrustLevel.UNVERIFIED),
    # 短前缀(便于人工书写)
    ("law:", TrustLevel.OFFICIAL_LAW),
    ("gov:", TrustLevel.GOVERNMENT_DOC),
    ("case:", TrustLevel.COURT_CASE),
    ("lawyer:", TrustLevel.LAWYER_VERIFIED),
    ("user:", TrustLevel.USER_EXPERIENCE),
    ("ai:", TrustLevel.AI_GENERATED),
]


@dataclass
class TrustRecord:
    """单个来源的信任度记录。

    Attributes:
        source: 来源标识(如 "official_law:cn")
        level: 信任等级
        score: 当前信任分数(0.0-1.0)
        last_updated: 最后更新时间(epoch)
        history: 历史调整记录 [(timestamp, delta, reason)]
    """

    source: str
    level: TrustLevel = TrustLevel.UNVERIFIED
    score: float = 0.0
    last_updated: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["level"] = self.level.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrustRecord":
        return cls(
            source=data["source"],
            level=TrustLevel(data.get("level", "unverified")),
            score=float(data.get("score", 0.0)),
            last_updated=float(data.get("last_updated", time.time())),
            history=list(data.get("history", []) or []),
        )


def _classify_source(source: str) -> TrustLevel:
    """根据 source 字符串前缀推断 TrustLevel。

    推断规则:
        1. 显式前缀匹配(如 "official_law:...")
        2. 未命中任何前缀 → UNVERIFIED
    """
    if not source:
        return TrustLevel.UNVERIFIED
    src_lower = source.lower()
    for prefix, level in _SOURCE_PREFIX_MAP:
        if src_lower.startswith(prefix):
            return level
    return TrustLevel.UNVERIFIED


class TrustScorer:
    """知识源信任度评分器。

    用法:
        scorer = TrustScorer()
        s = scorer.score("official_law:cn")          # 0.95
        scorer.update("user:x", delta=-0.1, reason="用户反馈错误")
        agg = scorer.aggregate([0.95, 0.85, 0.50])   # 加权聚合

    设计:
        - 7 个预设等级(TrustLevel),也可通过 update 动态调整
        - score() 先查动态调整记录,没有则按等级默认值返回
        - aggregate 加权:低置信度(偏离群体)评分权重更高,体现"少数意见不被淹没"
        - 持久化可选:不传 persist_path 则纯内存
    """

    def __init__(self, persist_path: Optional[Path] = None) -> None:
        self.persist_path = persist_path
        self._lock = threading.RLock()
        self._records: dict[str, TrustRecord] = {}
        if persist_path is not None:
            self._load()

    # ==================================================================
    # 评分查询
    # ==================================================================

    def score(self, source: str) -> float:
        """查询某来源的信任分数(0.0-1.0)。

        优先级:
            1. 动态调整记录(history 非空)→ 返回记录的当前 score
            2. 等级默认分数(_DEFAULT_TRUST_SCORES)

        Args:
            source: 来源标识(如 "official_law:cn")

        Returns:
            信任分数 0.0-1.0;flag 关闭时返回 UNVERIFIED 默认 0.20
        """
        if not is_enabled("knowledge_graph"):
            return _DEFAULT_TRUST_SCORES[TrustLevel.UNVERIFIED]

        with self._lock:
            record = self._records.get(source)
            if record is not None and record.history:
                # 有调整记录 → 用当前动态分数
                return max(0.0, min(1.0, record.score))
            # 无调整记录 → 按等级默认
            level = _classify_source(source)
            return _DEFAULT_TRUST_SCORES[level]

    def get_level(self, source: str) -> TrustLevel:
        """查询某来源的信任等级。"""
        with self._lock:
            record = self._records.get(source)
            if record is not None:
                return record.level
            return _classify_source(source)

    def get_record(self, source: str) -> Optional[TrustRecord]:
        """获取完整信任记录(含 history)。"""
        with self._lock:
            return self._records.get(source)

    # ==================================================================
    # 评分更新
    # ==================================================================

    def update(
        self,
        source: str,
        delta: float,
        reason: str = "",
    ) -> float:
        """基于用户反馈调整信任分数。

        Args:
            source: 来源标识
            delta: 调整量(正数=提升信任,负数=降低信任)
            reason: 调整原因(审计用)

        Returns:
            调整后的新分数(0.0-1.0)
        """
        if not is_enabled("knowledge_graph"):
            return _DEFAULT_TRUST_SCORES[TrustLevel.UNVERIFIED]

        with self._lock:
            level = _classify_source(source)
            record = self._records.get(source)
            if record is None:
                # 首次创建:用等级默认值初始化
                record = TrustRecord(
                    source=source,
                    level=level,
                    score=_DEFAULT_TRUST_SCORES[level],
                )
                self._records[source] = record

            new_score = max(0.0, min(1.0, record.score + delta))
            old_score = record.score
            record.score = new_score
            record.last_updated = time.time()
            record.history.append({
                "timestamp": record.last_updated,
                "delta": delta,
                "old_score": old_score,
                "new_score": new_score,
                "reason": reason or "",
            })
            self._persist()
            logger.info(
                "Trust updated: %s %.3f → %.3f (delta=%.3f, reason=%s)",
                source, old_score, new_score, delta, reason,
            )
            return new_score

    def reset(self, source: str) -> float:
        """重置来源到等级默认分数(清除动态调整)。"""
        with self._lock:
            if source in self._records:
                del self._records[source]
                self._persist()
            level = _classify_source(source)
            return _DEFAULT_TRUST_SCORES[level]

    # ==================================================================
    # 聚合
    # ==================================================================

    @staticmethod
    def aggregate(scores: list[float]) -> float:
        """加权聚合多个来源的分数。

        加权策略:
            - 权重 = 1.0 + |score - mean|:偏离群体越远权重越高
            - 这样低置信度(少数派)的意见不被淹没,
              与传统简单加权(高信任=高权重)相反,适合"少数意见纳入考虑"
            - 空列表 → 0.0

        Args:
            scores: 多个来源的信任分数

        Returns:
            聚合后的信任分数(0.0-1.0)
        """
        if not scores:
            return 0.0
        if len(scores) == 1:
            return max(0.0, min(1.0, scores[0]))

        mean = sum(scores) / len(scores)
        # 权重 = 1.0 + |score - mean|
        weighted_sum = 0.0
        weight_total = 0.0
        for s in scores:
            w = 1.0 + abs(s - mean)
            weighted_sum += s * w
            weight_total += w
        if weight_total == 0:
            return mean
        result = weighted_sum / weight_total
        return max(0.0, min(1.0, result))

    # ==================================================================
    # 工具方法
    # ==================================================================

    def list_sources(self) -> list[TrustRecord]:
        """列出所有已记录的来源。"""
        with self._lock:
            return list(self._records.values())

    def all_default_scores(self) -> dict[TrustLevel, float]:
        """返回等级默认分数表(便于测试 / UI 展示)。"""
        return dict(_DEFAULT_TRUST_SCORES)

    # ==================================================================
    # 持久化
    # ==================================================================

    def _persist(self) -> None:
        if self.persist_path is None:
            return
        try:
            data = {
                "version": 1,
                "updated_at": time.time(),
                "records": {s: r.to_dict() for s, r in self._records.items()},
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.persist_path)
        except OSError as e:
            logger.error("TrustScorer persist failed: %s", e)

    def _load(self) -> None:
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            text = self.persist_path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            for src, rec in data.get("records", {}).items():
                self._records[src] = TrustRecord.from_dict(rec)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("TrustScorer load failed, using empty: %s", e)


__all__ = [
    "TrustLevel",
    "TrustRecord",
    "TrustScorer",
    "_DEFAULT_TRUST_SCORES",
    "_classify_source",
]
