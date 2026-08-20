"""P8.4.3 用户评分系统 - rate / 查询 / 平均分 / helpful vote / flag。

设计:
    - Rating: 单条评分(score 1-5 + review_text + helpful_votes)
    - RatingFlag: 投诉记录(用户标记不当内容)
    - RatingSystem: 评分管理 + 持久化

去重规则:
    - 一个用户对一个 agent 只能有一条 Rating(再次 rate 视为更新,覆盖原值)
    - helpful_vote 一个用户对一个 rating 只能投一次

持久化:
    - `data/marketplace/ratings.json`(原子写,tenant-aware via resolve_data_path)

feature flag: `DEADMAN_MARKETPLACE_ENABLED=0`(默认关闭)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id, resolve_data_path
from .registry import MarketplaceError

logger = logging.getLogger(__name__)


# =====================================================================
# 数据模型
# =====================================================================
@dataclass
class Rating:
    """单条用户评分。

    Attributes:
        rating_id: 评分唯一 ID(rate 时自动生成 = "{agent_id}:{user_id}")
        agent_id: 被评 agent
        user_id: 评分者
        score: 1-5(整数)
        review_text: 评论文本(可选)
        created_at: epoch
        updated_at: 最后更新时间
        helpful_votes: 被点"有用"次数
        voted_by: 投过 useful 的 user_id 集合(去重用)
    """

    rating_id: str
    agent_id: str
    user_id: str
    score: int
    review_text: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    helpful_votes: int = 0
    voted_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rating:
        return cls(
            rating_id=data["rating_id"],
            agent_id=data["agent_id"],
            user_id=data["user_id"],
            score=int(data["score"]),
            review_text=data.get("review_text", ""),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            helpful_votes=int(data.get("helpful_votes", 0)),
            voted_by=list(data.get("voted_by", []) or []),
        )


@dataclass
class RatingFlag:
    """用户投诉记录(举报不当内容)。"""

    flag_id: str
    agent_id: str
    user_id: str
    reason: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RatingFlag:
        return cls(
            flag_id=data["flag_id"],
            agent_id=data["agent_id"],
            user_id=data["user_id"],
            reason=data.get("reason", ""),
            created_at=float(data.get("created_at", time.time())),
        )


# =====================================================================
# RatingSystem
# =====================================================================
class RatingSystem:
    """评分系统。

    线程安全: 单实例 RLock + 原子 os.replace。
    多租户: 通过 resolve_data_path 自动隔离。
    """

    DEFAULT_STORE_REL = "marketplace/ratings.json"

    def __init__(self, store_path: Any | None = None) -> None:
        self._explicit_store = store_path
        self._lock = threading.RLock()
        # ratings: {rating_id: Rating}
        self._ratings: dict[str, Rating] = {}
        # flags: {flag_id: RatingFlag}
        self._flags: dict[str, RatingFlag] = {}
        # 当前 cache 对应的 store path(检测 tenant 切换)
        self._loaded_path: str | None = None

    # ==================================================================
    # 路径解析
    # ==================================================================
    def _resolve_store_path(self):
        if self._explicit_store is not None:
            return self._explicit_store
        return resolve_data_path(self.DEFAULT_STORE_REL)

    # ==================================================================
    # 评分
    # ==================================================================
    def rate(
        self,
        agent_id: str,
        user_id: str,
        score: int,
        review_text: str = "",
    ) -> Rating:
        """评分 / 更新评分。

        去重: 同一 user 对同一 agent 已有评分则覆盖(score + review_text),
              created_at 保留,updated_at 刷新,helpful_votes 保留。

        Args:
            agent_id: 被评 agent
            user_id: 评分者
            score: 1-5
            review_text: 评论文本(可选)

        Raises:
            MarketplaceError: flag 关闭 / score 越界
        """
        self._require_enabled()
        if not isinstance(score, int) or score < 1 or score > 5:
            raise MarketplaceError(f"score must be int in [1,5], got {score!r}")

        rating_id = f"{agent_id}:{user_id}"
        with self._lock:
            self._load()
            now = time.time()
            existing = self._ratings.get(rating_id)
            if existing is None:
                rating = Rating(
                    rating_id=rating_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    score=score,
                    review_text=review_text,
                    created_at=now,
                    updated_at=now,
                    helpful_votes=0,
                    voted_by=[],
                )
            else:
                # 去重更新:覆盖 score + review_text,保留 helpful_votes / voted_by
                existing.score = score
                existing.review_text = review_text
                existing.updated_at = now
                rating = existing
            self._ratings[rating_id] = rating
            self._save()
            logger.info(
                "Rating submitted: agent=%s user=%s score=%d tenant=%s",
                agent_id,
                user_id,
                score,
                get_current_tenant_id(),
            )
            return rating

    def get_ratings(self, agent_id: str) -> list[Rating]:
        """获取某 agent 的所有评分(按 created_at 倒序)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            results = [r for r in self._ratings.values() if r.agent_id == agent_id]
            results.sort(key=lambda r: r.created_at, reverse=True)
            return results

    def average_score(self, agent_id: str) -> float:
        """计算某 agent 的平均分(无评分返回 0.0)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            scores = [r.score for r in self._ratings.values() if r.agent_id == agent_id]
            if not scores:
                return 0.0
            return sum(scores) / len(scores)

    def helpful_vote(self, rating_id: str, voter_id: str) -> bool:
        """对某条评分点"有用"(一个用户对一条评分只能投一次)。

        Returns:
            True 投票成功; False 已投过 / rating 不存在
        """
        self._require_enabled()
        with self._lock:
            self._load()
            rating = self._ratings.get(rating_id)
            if rating is None:
                return False
            if voter_id in rating.voted_by:
                return False
            rating.voted_by.append(voter_id)
            rating.helpful_votes += 1
            self._save()
            return True

    def flag(self, agent_id: str, user_id: str, reason: str) -> bool:
        """举报某 agent 不当内容(同一 user 可多次 flag,每次记录)。"""
        self._require_enabled()
        with self._lock:
            self._load()
            now = time.time()
            flag_id = f"flag:{agent_id}:{user_id}:{int(now * 1000)}"
            flag = RatingFlag(
                flag_id=flag_id,
                agent_id=agent_id,
                user_id=user_id,
                reason=reason,
                created_at=now,
            )
            self._flags[flag_id] = flag
            self._save()
            logger.info(
                "Agent flagged: agent=%s user=%s reason=%s",
                agent_id,
                user_id,
                reason,
            )
            return True

    def get_flags(self, agent_id: str) -> list[RatingFlag]:
        """获取某 agent 的所有投诉记录。"""
        self._require_enabled()
        with self._lock:
            self._load()
            return [f for f in self._flags.values() if f.agent_id == agent_id]

    # ==================================================================
    # 内部: 持久化
    # ==================================================================
    def _load(self) -> None:
        store = self._resolve_store_path()
        store_key = str(store)
        if store_key == self._loaded_path:
            return
        try:
            new_ratings: dict[str, Rating] = {}
            new_flags: dict[str, RatingFlag] = {}
            if store.exists():
                text = store.read_text(encoding="utf-8")
                data = json.loads(text) if text.strip() else {}
                new_ratings = {
                    rid: Rating.from_dict(rd)
                    for rid, rd in (data.get("ratings", {}) or {}).items()
                    if isinstance(rd, dict) and "rating_id" in rd
                }
                new_flags = {
                    fid: RatingFlag.from_dict(fd)
                    for fid, fd in (data.get("flags", {}) or {}).items()
                    if isinstance(fd, dict) and "flag_id" in fd
                }
            self._ratings = new_ratings
            self._flags = new_flags
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Ratings store load failed (%s): %s", store, e)
            self._ratings = {}
            self._flags = {}
        self._loaded_path = store_key

    def _save(self) -> None:
        store = self._resolve_store_path()
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "tenant_id": get_current_tenant_id(),
                "ratings": {rid: r.to_dict() for rid, r in self._ratings.items()},
                "flags": {fid: f.to_dict() for fid, f in self._flags.items()},
            }
            tmp_path = store.with_suffix(store.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, store)
        except OSError as e:
            logger.error("Ratings store save failed (%s): %s", store, e)
            raise MarketplaceError(f"Ratings save failed: {e}") from e

    def _require_enabled(self) -> None:
        if not is_enabled("marketplace"):
            raise MarketplaceError(
                "Marketplace feature is disabled (set DEADMAN_MARKETPLACE_ENABLED=1)"
            )


# =====================================================================
# 全局单例
# =====================================================================
_rating_instance: RatingSystem | None = None
_rating_lock = threading.Lock()


def get_rating_system() -> RatingSystem:
    """获取全局 RatingSystem 单例。"""
    global _rating_instance
    if _rating_instance is None:
        with _rating_lock:
            if _rating_instance is None:
                _rating_instance = RatingSystem()
    return _rating_instance
