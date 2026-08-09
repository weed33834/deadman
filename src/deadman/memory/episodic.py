"""情景记忆 - 过往对话片段，按时间 + 语义双索引。

类比人回忆"上次什么时候说了什么"。
用内存 dict 模拟向量库；语义检索用关键词匹配模拟（不需要真正的 embedding）。
Graphiti 集成为可选项。

P2 增强:
    - P2.1 向量库接入:recall_by_semantic 在 VECTOR_STORE_ENABLED 时优先走向量库
    - P2.2 TTL + LRU:EPISODE_TTL_DAYS + EPISODE_MAX_COUNT + archived/last_accessed_at
    - P2.3 Graphiti 深度集成:recall_by_graphiti 用 Graphiti 真实图搜索
    - P2.6 遗忘曲线:forgetting_score 加权召回排序
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..config import settings
from ..llm import get_llm_for_use_case

logger = logging.getLogger(__name__)

# =====================================================================
# P2 feature flags - 默认全部关闭
# =====================================================================
EPISODIC_TTL_ENABLED: bool = os.environ.get("DEADMAN_EPISODIC_TTL_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

GRAPHITI_DEEP_ENABLED: bool = os.environ.get("DEADMAN_GRAPHITI_DEEP_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

FORGETTING_CURVE_ENABLED: bool = os.environ.get(
    "DEADMAN_FORGETTING_CURVE_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 向量库 feature flag(从 vector_store 模块导入避免重复读 env)
try:
    from .vector_store import VECTOR_STORE_ENABLED as _VS_FLAG
    from .vector_store import get_vector_store as _get_vector_store
except Exception:  # pragma: no cover - 极端情况
    _VS_FLAG = False

    def _get_vector_store(*a, **kw):  # type: ignore[no-redef,misc]
        return None


# =====================================================================
# P2.2 TTL + LRU 常量
# =====================================================================
EPISODE_TTL_DAYS: int = int(os.environ.get("DEADMAN_EPISODE_TTL_DAYS", "365"))
EPISODE_MAX_COUNT: int = int(os.environ.get("DEADMAN_EPISODE_MAX_COUNT", "10000"))
# archived 后多少天物理删除
EPISODE_ARCHIVE_GRACE_DAYS: int = 30

# P2.6 遗忘曲线衰减常数(30 天衰减到 37%)
FORGETTING_DECAY_DAYS: int = 30

# 简单中英文停用词，用于关键词过滤
_STOPWORDS = {
    "的",
    "了",
    "是",
    "在",
    "我",
    "你",
    "他",
    "她",
    "它",
    "和",
    "与",
    "及",
    "或",
    "也",
    "都",
    "就",
    "这",
    "那",
    "有",
    "没",
    "不",
    "要",
    "会",
    "能",
    "把",
    "被",
    "让",
    "给",
    "对",
    "向",
    "从",
    "到",
    "一个",
    "什么",
    "怎么",
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "as",
    "that",
    "this",
}


def _extract_keywords(text: str) -> list[str]:
    """简单关键词提取，用于模拟 embedding 语义检索。

    对中文长串额外生成 2 字滑动窗口，简易模拟分词效果。
    """
    if not text:
        return []
    # 匹配字母数字序列或汉字序列
    tokens = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text.lower())
    keywords: list[str] = []
    for t in tokens:
        if len(t) < 2 or t in _STOPWORDS:
            continue
        keywords.append(t)
        # 中文长串（>=4 字）额外生成 2 字滑动窗口，提升召回
        if len(t) >= 4 and re.match(r"[\u4e00-\u9fff]+", t):
            for i in range(len(t) - 1):
                window = t[i : i + 2]
                if window not in _STOPWORDS:
                    keywords.append(window)
    return keywords


@dataclass
class Episode:
    """一个情景片段"""

    episode_id: str
    session_id: str
    timestamp: datetime
    agent: str
    user_message: str
    assistant_response: str
    transfer_triggered: bool = False
    subagents_called: list[str] = field(default_factory=list)
    rule_check_result: dict | None = None
    risk_tier: str = "R0"
    summary: str = ""
    # 关键词列表，模拟 embedding 用于语义检索
    keywords: list[str] = field(default_factory=list)
    # === P2.2 TTL + LRU 字段(默认值保证旧路径不变) ===
    archived: bool = False
    last_accessed_at: datetime | None = None
    archived_at: datetime | None = None
    # === P2.6 遗忘曲线:重要性(0.0-1.0),默认 0.5 ===
    importance: float = 0.5


class EpisodicMemory:
    """情景记忆 - 过往对话片段，按时间 + 语义双索引。

    用内存 dict 模拟向量库：
        - _store: episode_id -> Episode  （主存储）
        - _by_session: session_id -> [episode_id, ...]  （会话时间索引）

    P2.2 TTL + LRU(EPISODIC_TTL_ENABLED 开启时生效):
        - TTL 过期标记 archived=True,EPISODE_ARCHIVE_GRACE_DAYS 天后物理删
        - 超过 EPISODE_MAX_COUNT 时按 last_accessed_at LRU 淘汰
    """

    def __init__(self, graphiti_client: Any = None):
        self._store: dict[str, Episode] = {}
        self._by_session: dict[str, list[str]] = {}
        self.graphiti = graphiti_client
        self._retention_years = settings.memory_retention_years
        # P2.1 向量库引用(惰性初始化)
        self._vector_store: Any = None
        self._vector_store_initialized: bool = False

    # ==================================================================
    # P2.1 向量库惰性获取
    # ==================================================================
    @property
    def vector_store(self) -> Any:
        """惰性获取向量库单例。VECTOR_STORE_ENABLED=0 时返回 None。"""
        if not self._vector_store_initialized:
            try:
                self._vector_store = _get_vector_store()
            except Exception as exc:  # pragma: no cover - 极端情况
                logger.warning("向量库初始化失败: %s", exc)
                self._vector_store = None
            self._vector_store_initialized = True
        return self._vector_store

    async def archive_turn(self, session_id: str, turn: dict) -> Episode | None:
        """把工作记忆溢出的轮次归档到情景记忆。

        流程：生成摘要 -> 提取关键词（模拟 embedding） -> 存入内存 dict ->
        可选同步到 Graphiti。
        """
        content = turn.get("content", "")
        # 1. 生成摘要（LLM，失败回退到截断）
        summary = await self._summarize_turn(turn)
        # 2. 提取关键词（模拟 embedding）
        keywords = _extract_keywords(content) + _extract_keywords(summary)

        # 3. 构造 Episode 并存入内存 dict
        now = datetime.now(timezone.utc)
        episode = Episode(
            episode_id=turn.get("turn_id") or str(uuid4()),
            session_id=session_id,
            timestamp=turn.get("timestamp") or now,
            agent=turn.get("agent", "unknown"),
            user_message=content if turn.get("role") == "user" else "",
            assistant_response=content if turn.get("role") == "assistant" else "",
            transfer_triggered=turn.get("transfer_triggered", False),
            subagents_called=turn.get("subagent_called", turn.get("subagents_called", [])),
            rule_check_result=turn.get("rule_check_result"),
            risk_tier=turn.get("risk_tier", "R0"),
            summary=summary,
            keywords=list(set(keywords)),
            last_accessed_at=now,
            importance=float(turn.get("importance", 0.5)),
        )

        self._store[episode.episode_id] = episode
        self._by_session.setdefault(session_id, []).append(episode.episode_id)

        # P2.2 TTL + LRU(仅在 EPISODIC_TTL_ENABLED 启用时执行)
        if EPISODIC_TTL_ENABLED:
            try:
                self._apply_ttl_filter()
                self._apply_lru_eviction()
            except Exception as exc:  # pragma: no cover - 韧性
                logger.warning("TTL/LRU 维护失败: %s", exc)

        # P2.1 向量库同步(VECTOR_STORE_ENABLED 启用时)
        if _VS_FLAG and self.vector_store is not None:
            try:
                text_for_vec = episode.summary or episode.user_message or episode.assistant_response
                self.vector_store.add(
                    id=episode.episode_id,
                    text=text_for_vec,
                    metadata={
                        "session_id": session_id,
                        "agent": episode.agent,
                        "timestamp": episode.timestamp.isoformat() if episode.timestamp else "",
                    },
                )
            except Exception as exc:
                logger.warning("向量库同步失败,降级关键词匹配: %s", exc)

        # 4. 可选：同步到 Graphiti（时态记忆）
        if self.graphiti is not None:
            try:
                self.graphiti.add_event(
                    {
                        "event_type": "UserProgressEvent",
                        "episode_id": episode.episode_id,
                        "session_id": session_id,
                        "timestamp": episode.timestamp,
                        "summary": summary,
                        "agent": episode.agent,
                    }
                )
            except Exception as e:
                logger.warning(f"Graphiti 同步失败: {e}")

        return episode

    def recall_by_time(self, session_id: str, start: datetime, end: datetime) -> list[Episode]:
        """按时间范围回忆"""
        result: list[Episode] = []
        for eid in self._by_session.get(session_id, []):
            ep = self._store.get(eid)
            if ep is None:
                continue
            # P2.2 archived 不召回(仅在 TTL 启用时生效)
            if EPISODIC_TTL_ENABLED and ep.archived:
                continue
            if start <= ep.timestamp <= end:
                self._touch_access(ep)
                result.append(ep)
        result.sort(key=lambda e: e.timestamp)
        return result

    def recall_recent(self, session_id: str, n: int = 5) -> list[Episode]:
        """回忆最近 N 个片段（按时间倒序）"""
        ids = self._by_session.get(session_id, [])
        # 按 timestamp 倒序
        ranked = sorted(
            ids,
            key=lambda eid: self._store[eid].timestamp if eid in self._store else datetime.min,
            reverse=True,
        )
        out: list[Episode] = []
        for eid in ranked:
            ep = self._store.get(eid)
            if ep is None:
                continue
            # P2.2 archived 不召回
            if EPISODIC_TTL_ENABLED and ep.archived:
                continue
            self._touch_access(ep)
            out.append(ep)
            if len(out) >= n:
                break
        return out

    def recall_by_semantic(
        self,
        query: str,
        top_k: int = 5,
        session_id: str | None = None,
    ) -> list[Episode]:
        """按语义相似度回忆。

        优先级:
            1. P2.1 向量库(VECTOR_STORE_ENABLED and self.vector_store)
            2. Graphiti 可用时优先用其语义搜索(真实 embedding)
            3. 降级：关键词匹配模拟

        Args:
            query: 查询文本
            top_k: 返回前 K 个最相关片段
            session_id: 可选，限定会话范围
        """
        # P2.1 向量库优先(VECTOR_STORE_ENABLED 启用且单例就绪)
        if _VS_FLAG and self.vector_store is not None:
            try:
                vec_results = self.vector_store.query(query, top_k=top_k * 2)
                episodes: list[Episode] = []
                for hit in vec_results:
                    eid = hit.get("id")
                    if not eid:
                        continue
                    ep = self._store.get(str(eid))
                    if ep is None:
                        continue
                    # P2.2 archived 不召回
                    if EPISODIC_TTL_ENABLED and ep.archived:
                        continue
                    if session_id and ep.session_id != session_id:
                        continue
                    self._touch_access(ep)
                    episodes.append(ep)
                    if len(episodes) >= top_k:
                        break
                if episodes:
                    # P2.6 遗忘曲线加权重排
                    if FORGETTING_CURVE_ENABLED:
                        episodes.sort(key=lambda e: self.forgetting_score(e), reverse=True)
                    return episodes
                # 向量库查询无命中,继续走降级路径
            except Exception as exc:
                logger.warning("向量库查询失败,降级关键词匹配: %s", exc)

        # Graphiti 可用时优先用其语义搜索
        if self.graphiti is not None:
            graphiti_results = self._graphiti_search(query, top_k)
            if graphiti_results:
                # P2.6 遗忘曲线加权重排
                if FORGETTING_CURVE_ENABLED:
                    graphiti_results.sort(key=lambda e: self.forgetting_score(e), reverse=True)
                return graphiti_results

        # 降级：关键词匹配模拟
        query_kw = set(_extract_keywords(query))
        if not query_kw:
            return []

        scored: list[tuple[float, Episode]] = []
        for ep in self._store.values():
            if session_id and ep.session_id != session_id:
                continue
            # P2.2 archived 不召回
            if EPISODIC_TTL_ENABLED and ep.archived:
                continue
            # 匹配预提取的关键词 + 现场提取 summary/user_message 关键词
            ep_kw = set(ep.keywords)
            ep_kw |= set(_extract_keywords(ep.summary))
            ep_kw |= set(_extract_keywords(ep.user_message))
            overlap = len(query_kw & ep_kw)
            if overlap > 0:
                # P2.6 遗忘曲线加权
                if FORGETTING_CURVE_ENABLED:
                    score = overlap * self.forgetting_score(ep)
                else:
                    score = float(overlap)
                scored.append((score, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        out = [ep for _, ep in scored[:top_k]]
        for ep in out:
            self._touch_access(ep)
        return out

    # ==================================================================
    # P2.3 Graphiti 深度集成 - 真实图搜索
    # ==================================================================
    async def recall_by_graphiti(self, query: str, top_k: int = 3) -> list[Episode]:
        """用 Graphiti 真实图搜索(深度集成,非 fallback)。

        降级链:
            1. GRAPHITI_DEEP_ENABLED=0 → 走 recall_by_semantic
            2. self.graphiti is None → 走 recall_by_semantic
            3. Graphiti.search 抛错 → 走 recall_by_semantic
        """
        if not GRAPHITI_DEEP_ENABLED:
            return self.recall_by_semantic(query, top_k=top_k)
        if self.graphiti is None:
            logger.debug("Graphiti 不可用,recall_by_graphiti 降级到语义召回")
            return self.recall_by_semantic(query, top_k=top_k)
        try:
            import inspect

            raw = self.graphiti.search(query, num_results=top_k)
            if inspect.isawaitable(raw):
                try:
                    raw = await raw  # type: ignore[assignment]
                except RuntimeError:
                    # 已在事件循环中且无法 await,降级
                    return self.recall_by_semantic(query, top_k=top_k)
            if not isinstance(raw, list):
                return self.recall_by_semantic(query, top_k=top_k)
            episodes = self._graphiti_results_to_episodes(raw)
            if not episodes:
                return self.recall_by_semantic(query, top_k=top_k)
            # P2.6 遗忘曲线加权重排
            if FORGETTING_CURVE_ENABLED:
                episodes.sort(key=lambda e: self.forgetting_score(e), reverse=True)
            for ep in episodes:
                self._touch_access(ep)
            return episodes
        except Exception as exc:
            logger.warning("Graphiti 深度搜索失败,降级到语义召回: %s", exc)
            return self.recall_by_semantic(query, top_k=top_k)

    def _graphiti_search(self, query: str, top_k: int) -> list[Episode]:
        """尝试用 Graphiti 语义搜索，安全降级。

        Graphiti 的 search 可能是同步或异步，这里统一处理。
        在异步上下文中（如 MCP Server 的 asyncio.run 内）不阻塞，返回空列表降级。
        """
        if self.graphiti is None:
            return []
        try:
            import inspect

            raw = self.graphiti.search(query, num_results=top_k)
            # 处理可能的异步返回
            if inspect.isawaitable(raw):
                import asyncio

                try:
                    asyncio.get_running_loop()
                    # 已在异步事件循环中，不能阻塞，降级
                    return []
                except RuntimeError:
                    raw = asyncio.run(raw)  # type: ignore[arg-type]

            if not isinstance(raw, list):
                return []
            return self._graphiti_results_to_episodes(raw)
        except Exception as e:
            logger.warning(f"Graphiti 查询失败，降级到关键词匹配: {e}")
            return []

    def _graphiti_results_to_episodes(self, raw: list) -> list[Episode]:
        """把 Graphiti 原始结果转为 Episode 列表"""
        episodes: list[Episode] = []
        for item in raw:
            if isinstance(item, dict):
                data = item
            else:
                data = vars(item) if hasattr(item, "__dict__") else {}
            ts = data.get("created_at") or data.get("timestamp")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    ts = datetime.now(timezone.utc)
            elif ts is None:
                ts = datetime.now(timezone.utc)

            episodes.append(
                Episode(
                    episode_id=str(data.get("uuid", data.get("id", ""))),
                    session_id=str(data.get("session_id", "")),
                    timestamp=ts,
                    agent=str(data.get("agent", "graphiti")),
                    user_message="",
                    assistant_response=str(data.get("content", data.get("summary", ""))),
                    summary=str(data.get("summary", data.get("content", ""))),
                )
            )
        return episodes

    # ==================================================================
    # P2.2 TTL + LRU 实现
    # ==================================================================
    def _touch_access(self, episode: Episode) -> None:
        """更新 last_accessed_at(仅 TTL 启用时,关闭时旧路径不调用)"""
        if EPISODIC_TTL_ENABLED:
            episode.last_accessed_at = datetime.now(timezone.utc)

    def _apply_ttl_filter(self, now: datetime | None = None) -> int:
        """TTL 过滤:过期 episode 标记 archived=True,30 天后才物理删。

        Returns:
            本次标记 archived 的数量(物理删除的不计入)
        """
        if not EPISODIC_TTL_ENABLED:
            return 0
        if now is None:
            now = datetime.now(timezone.utc)
        ttl_cutoff = now - timedelta(days=EPISODE_TTL_DAYS)
        archive_purge_cutoff = now - timedelta(days=EPISODE_ARCHIVE_GRACE_DAYS)
        marked = 0
        to_purge: list[str] = []
        for eid, ep in self._store.items():
            # 已 archived 超过 grace 期 → 物理删
            if ep.archived:
                archived_at = ep.archived_at or ep.last_accessed_at or ep.timestamp
                if archived_at < archive_purge_cutoff:
                    to_purge.append(eid)
                continue
            # 未 archived 但 timestamp 过 TTL → 标记 archived
            # (用 timestamp 作 TTL 基准,反映"创建时间")
            ts = ep.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < ttl_cutoff:
                ep.archived = True
                ep.archived_at = now
                marked += 1
        # 物理删除 archived 已超 grace 期的
        for eid in to_purge:
            self._purge_episode(eid)
        return marked

    def _apply_lru_eviction(self) -> int:
        """LRU 淘汰:超过 EPISODE_MAX_COUNT 时按 last_accessed_at 淘汰最久未访问。

        archived episode 优先被淘汰(它们已不在召回路径中)。
        Returns:
            本次淘汰数量
        """
        if not EPISODIC_TTL_ENABLED:
            return 0
        if len(self._store) <= EPISODE_MAX_COUNT:
            return 0
        excess = len(self._store) - EPISODE_MAX_COUNT
        datetime.now(timezone.utc)

        def _access_ts(ep: Episode) -> datetime:
            ts = ep.last_accessed_at or ep.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts

        # 排序:archived 优先淘汰;同状态按 last_accessed_at 升序
        candidates = sorted(
            self._store.items(),
            key=lambda kv: (not kv[1].archived, _access_ts(kv[1])),
        )
        evicted = 0
        for eid, _ep in candidates:
            if evicted >= excess:
                break
            self._purge_episode(eid)
            evicted += 1
        return evicted

    def _purge_episode(self, episode_id: str) -> None:
        """物理删除一个 episode(从 _store + _by_session + 向量库)"""
        ep = self._store.pop(episode_id, None)
        if ep is None:
            return
        # 从会话索引移除
        sess = ep.session_id
        if sess in self._by_session:
            with contextlib.suppress(ValueError):
                self._by_session[sess].remove(episode_id)
            if not self._by_session[sess]:
                del self._by_session[sess]
        # 从向量库移除
        if _VS_FLAG and self.vector_store is not None:
            try:
                self.vector_store.delete(episode_id)
            except Exception as exc:  # pragma: no cover - 韧性
                logger.warning("向量库删除失败: %s", exc)

    # ==================================================================
    # P2.6 遗忘曲线(Ebbinghaus)
    # ==================================================================
    def forgetting_score(self, episode: Episode) -> float:
        """遗忘曲线评分:score = importance * exp(-delta_days / 30)

        Args:
            episode: 单个片段

        Returns:
            0.0-1.0 之间的遗忘加权分;近期+高重要性 → 接近 importance,
            30 天前 → importance * 0.37,60 天前 → importance * 0.13。
            FORGETTING_CURVE_ENABLED=0 时返回 importance(无衰减)。
        """
        importance = float(getattr(episode, "importance", 0.5) or 0.5)
        importance = max(0.0, min(1.0, importance))
        if not FORGETTING_CURVE_ENABLED:
            return importance
        now = datetime.now(timezone.utc)
        ref = episode.last_accessed_at or episode.timestamp
        if ref is None:
            return importance
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        delta_days = max(0.0, (now - ref).total_seconds() / 86400.0)
        decay = math.exp(-delta_days / float(FORGETTING_DECAY_DAYS))
        return importance * decay

    async def _summarize_turn(self, turn: dict) -> str:
        """用 LLM 生成片段摘要；无 API key 或失败时回退到简单截断

        P7: 多模型分工 - 摘要归入 summarizer 用例（便宜模型），借鉴 OpenDeepResearch。
        """
        content = turn.get("content", "")
        summarizer_llm = get_llm_for_use_case("summarizer")
        if not summarizer_llm.api_key:
            return self._fallback_summary(turn)
        prompt = (
            "用一句话总结以下对话片段的核心信息（包含关键事实如人物/时间/地点/事件，"
            "不超过 50 字，用于后续检索）：\n"
            f"角色：{turn.get('role', 'unknown')}\n"
            f"内容：{content}\n"
            f"智能体：{turn.get('agent', 'unknown')}\n"
        )
        try:
            text = await summarizer_llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=120,
            )
            return text.strip()
        except Exception as e:
            logger.warning(f"LLM 摘要失败，使用回退: {e}")
            return self._fallback_summary(turn)

    @staticmethod
    def _fallback_summary(turn: dict) -> str:
        """回退摘要：截断内容前 50 字"""
        content = turn.get("content", "")
        role = "用户" if turn.get("role") == "user" else "助手"
        snippet = content[:50].replace("\n", " ")
        return f"{role}: {snippet}"
