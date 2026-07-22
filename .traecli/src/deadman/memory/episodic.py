"""情景记忆 - 过往对话片段，按时间 + 语义双索引。

类比人回忆"上次什么时候说了什么"。
用内存 dict 模拟向量库；语义检索用关键词匹配模拟（不需要真正的 embedding）。
Graphiti 集成为可选项。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..config import settings
from ..llm import get_llm_for_use_case

logger = logging.getLogger(__name__)

# 简单中英文停用词，用于关键词过滤
_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "和", "与", "及",
    "或", "也", "都", "就", "这", "那", "有", "没", "不", "要", "会", "能",
    "把", "被", "让", "给", "对", "向", "从", "到", "一个", "什么", "怎么",
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they", "and", "or", "but", "in",
    "on", "at", "to", "for", "of", "with", "by", "from", "as", "that", "this",
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
    rule_check_result: Optional[dict] = None
    risk_tier: str = "R0"
    summary: str = ""
    # 关键词列表，模拟 embedding 用于语义检索
    keywords: list[str] = field(default_factory=list)


class EpisodicMemory:
    """情景记忆 - 过往对话片段，按时间 + 语义双索引。

    用内存 dict 模拟向量库：
        - _store: episode_id -> Episode  （主存储）
        - _by_session: session_id -> [episode_id, ...]  （会话时间索引）
    """

    def __init__(self, graphiti_client: Any = None):
        self._store: dict[str, Episode] = {}
        self._by_session: dict[str, list[str]] = {}
        self.graphiti = graphiti_client
        self._retention_years = settings.memory_retention_years

    async def archive_turn(self, session_id: str, turn: dict) -> Optional[Episode]:
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
        episode = Episode(
            episode_id=turn.get("turn_id") or str(uuid4()),
            session_id=session_id,
            timestamp=turn.get("timestamp") or datetime.now(timezone.utc),
            agent=turn.get("agent", "unknown"),
            user_message=content if turn.get("role") == "user" else "",
            assistant_response=content if turn.get("role") == "assistant" else "",
            transfer_triggered=turn.get("transfer_triggered", False),
            subagents_called=turn.get(
                "subagent_called", turn.get("subagents_called", [])
            ),
            rule_check_result=turn.get("rule_check_result"),
            risk_tier=turn.get("risk_tier", "R0"),
            summary=summary,
            keywords=list(set(keywords)),
        )

        self._store[episode.episode_id] = episode
        self._by_session.setdefault(session_id, []).append(episode.episode_id)

        # 4. 可选：同步到 Graphiti（时态记忆）
        if self.graphiti is not None:
            try:
                self.graphiti.add_event({
                    "event_type": "UserProgressEvent",
                    "episode_id": episode.episode_id,
                    "session_id": session_id,
                    "timestamp": episode.timestamp,
                    "summary": summary,
                    "agent": episode.agent,
                })
            except Exception as e:
                logger.warning(f"Graphiti 同步失败: {e}")

        return episode

    def recall_by_time(
        self, session_id: str, start: datetime, end: datetime
    ) -> list[Episode]:
        """按时间范围回忆"""
        result: list[Episode] = []
        for eid in self._by_session.get(session_id, []):
            ep = self._store.get(eid)
            if ep is None:
                continue
            if start <= ep.timestamp <= end:
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
        return [self._store[eid] for eid in ranked[:n] if eid in self._store]

    def recall_by_semantic(
        self,
        query: str,
        top_k: int = 5,
        session_id: str | None = None,
    ) -> list[Episode]:
        """按语义相似度回忆。

        Graphiti 可用时优先用其语义搜索（真实 embedding），
        不可用或失败时降级为关键词匹配模拟。

        Args:
            query: 查询文本
            top_k: 返回前 K 个最相关片段
            session_id: 可选，限定会话范围
        """
        # Graphiti 可用时优先用其语义搜索
        if self.graphiti is not None:
            graphiti_results = self._graphiti_search(query, top_k)
            if graphiti_results:
                return graphiti_results

        # 降级：关键词匹配模拟
        query_kw = set(_extract_keywords(query))
        if not query_kw:
            return []

        scored: list[tuple[int, Episode]] = []
        for ep in self._store.values():
            if session_id and ep.session_id != session_id:
                continue
            # 匹配预提取的关键词 + 现场提取 summary/user_message 关键词
            ep_kw = set(ep.keywords)
            ep_kw |= set(_extract_keywords(ep.summary))
            ep_kw |= set(_extract_keywords(ep.user_message))
            overlap = len(query_kw & ep_kw)
            if overlap > 0:
                scored.append((overlap, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [ep for _, ep in scored[:top_k]]

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
                    raw = asyncio.run(raw)

            if not isinstance(raw, list):
                return []

            # 将 Graphiti 结果转为 Episode
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
                        assistant_response=str(
                            data.get("content", data.get("summary", ""))
                        ),
                        summary=str(data.get("summary", data.get("content", ""))),
                    )
                )
            return episodes
        except Exception as e:
            logger.warning(f"Graphiti 查询失败，降级到关键词匹配: {e}")
            return []

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
