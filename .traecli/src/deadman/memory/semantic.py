"""语义记忆 - 用户画像 + 事实知识，结构化。

类比人的知识库："我爸叫张三，是北京人"这类事实。
含矛盾检测：字段旧值与新值不同时触发 _handle_contradiction。
Graphiti / LightRAG 集成为可选项。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from .working import WorkingMemory

logger = logging.getLogger(__name__)


@dataclass
class UserProfile:
    """用户画像 - 语义记忆的核心"""

    user_id: str
    name: Optional[str] = None
    relationship_to_deceased: Optional[str] = None  # 子女/配偶/父母/...
    location: Optional[dict] = None  # {country, region, city}
    deceased_info: Optional[dict] = None
    # {name, death_date, death_location, cause, nationality, domicile}
    family_structure: Optional[dict] = None
    # {spouse:{...}, children:[...], parents:[...]}
    assets_summary: Optional[dict] = None
    # {real_estate_count, has_bank_accounts, has_will, ...}
    current_stage: Optional[int] = None  # 9 阶段中的第几阶段
    completed_stages: list[int] = field(default_factory=list)
    pending_tasks: list[str] = field(default_factory=list)


@dataclass
class Fact:
    """事实知识 - 语义记忆的单元"""

    fact_id: str
    fact_type: str  # user_profile / policy / procedure / entity
    content: str
    source: str = "unknown"  # 来源：用户告知 / 知识库检索 / 政策搜索
    confidence: float = 0.5
    jurisdiction: Optional[dict] = None
    valid_time: Optional[dict] = None  # 与 Graphiti 集成的时态信息
    verified: bool = False  # 是否经过 integrity check
    supersedes: Optional[str] = None  # 取代了哪条旧事实


class SemanticMemory:
    """语义记忆 - 结构化事实知识。

    - user_profiles: user_id -> UserProfile
    - facts: fact_id -> Fact
    - pending_contradictions: 待澄清的矛盾列表
    """

    def __init__(
        self,
        graphiti_client: Any = None,
        lightrag_client: Any = None,
    ):
        self.user_profiles: dict[str, UserProfile] = {}
        self.facts: dict[str, Fact] = {}
        self.pending_contradictions: list[dict] = []
        self.graphiti = graphiti_client
        self.lightrag = lightrag_client
        # 工作记忆引用，由 MemoryManager 注入；矛盾告警注入到其 temp_vars
        self._working_memory: Optional[WorkingMemory] = None

    def set_working_memory(self, working_memory: WorkingMemory) -> None:
        """注入工作记忆引用，用于矛盾告警注入"""
        self._working_memory = working_memory

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        """获取用户画像"""
        return self.user_profiles.get(user_id)

    def update_user_profile(self, user_id: str, updates: dict) -> None:
        """更新用户画像，含矛盾检测。

        - dict 字段做字段级合并与矛盾检测
        - list 字段做去重合并
        - 标量字段直接比较，不同则触发矛盾
        """
        if not updates:
            return
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = UserProfile(user_id=user_id)

        profile = self.user_profiles[user_id]
        for key, value in updates.items():
            if value is None or not hasattr(profile, key):
                continue

            old_value = getattr(profile, key)
            if old_value is None:
                setattr(profile, key, value)
                continue

            # dict 类型：字段级合并与矛盾检测
            if isinstance(old_value, dict) and isinstance(value, dict):
                for sub_key, sub_new in value.items():
                    sub_old = old_value.get(sub_key)
                    if sub_old is not None and sub_old != sub_new:
                        self._handle_contradiction(
                            user_id, f"{key}.{sub_key}", sub_old, sub_new
                        )
                merged = {**old_value, **value}
                setattr(profile, key, merged)
                continue

            # list 类型：去重合并
            if isinstance(old_value, list) and isinstance(value, list):
                merged = list(old_value)
                for v in value:
                    if v not in merged:
                        merged.append(v)
                setattr(profile, key, merged)
                continue

            # 标量类型：直接比较
            if old_value != value:
                self._handle_contradiction(user_id, key, old_value, value)
            setattr(profile, key, value)

        # 可选：同步到 Graphiti（时态记忆，保留历史版本）
        if self.graphiti is not None:
            try:
                self.graphiti.add_event({
                    "event_type": "UserProgressEvent",
                    "user_id": user_id,
                    "profile_update": updates,
                    "timestamp": datetime.now(timezone.utc),
                })
            except Exception as e:
                logger.warning(f"Graphiti 同步失败: {e}")

    def _handle_contradiction(
        self,
        user_id: str,
        field_name: str,
        old_value: Any,
        new_value: Any,
    ) -> None:
        """用户前后矛盾检测 - integrity-framework 第三章：立刻提出，不猜测。

        记录矛盾并注入到工作记忆，让智能体在下一轮提出澄清。
        """
        contradiction = {
            "user_id": user_id,
            "field": field_name,
            "old_value": old_value,
            "new_value": new_value,
            "detected_at": datetime.now(timezone.utc),
        }
        self.pending_contradictions.append(contradiction)
        # 注入到工作记忆，让智能体在下一轮提出
        if self._working_memory is not None:
            try:
                self._working_memory.add_contradiction_alert(contradiction)
            except Exception as e:
                logger.warning(f"注入矛盾告警失败: {e}")
        logger.info(
            f"检测到矛盾 user={user_id} field={field_name}: "
            f"{old_value!r} -> {new_value!r}"
        )

    def add_fact(self, fact: Fact) -> None:
        """添加事实知识。

        若 fact.supersedes 指向旧事实，标记旧事实的 valid_time.valid_to。
        """
        # 若有旧版本被取代，标记其失效时间
        if fact.supersedes and fact.supersedes in self.facts:
            old_fact = self.facts[fact.supersedes]
            old_valid = dict(old_fact.valid_time or {})
            old_valid["valid_to"] = datetime.now(timezone.utc)
            old_fact.valid_time = old_valid

        self.facts[fact.fact_id] = fact

        # 可选：同步到 Graphiti（PolicyFact / KnowledgeVersion 类型，时态管理）
        if self.graphiti is not None:
            try:
                event_type = (
                    "PolicyFact" if fact.fact_type == "policy" else "KnowledgeVersion"
                )
                self.graphiti.add_event({
                    "event_type": event_type,
                    "fact_id": fact.fact_id,
                    "content": fact.content,
                    "source": fact.source,
                    "confidence": fact.confidence,
                    "valid_time": fact.valid_time,
                    "supersedes": fact.supersedes,
                    "transaction_time": datetime.now(timezone.utc),
                })
            except Exception as e:
                logger.warning(f"Graphiti 同步失败: {e}")

    def query_facts(
        self,
        query: str,
        jurisdiction: dict | None = None,
        fact_type: str | None = None,
    ) -> list[Fact]:
        """查询事实知识。

        1. 先在本地 facts 中精确匹配（关键词包含）
        2. 若本地不足 3 条，调用 LightRAG 知识图谱（可选）
        """
        results: list[Fact] = []
        q = query.lower()
        for fact in self.facts.values():
            if fact_type and fact.fact_type != fact_type:
                continue
            if jurisdiction and fact.jurisdiction != jurisdiction:
                continue
            if q and q in fact.content.lower():
                results.append(fact)

        # 本地不足时尝试 LightRAG
        if len(results) < 3 and self.lightrag is not None:
            try:
                graph_results = self.lightrag.query(query, mode="hybrid")
                for r in graph_results:
                    results.append(Fact(
                        fact_id=str(uuid4()),
                        fact_type="entity",
                        content=r.get("content", ""),
                        source=r.get("source", "lightrag"),
                        confidence=float(r.get("confidence", 0.7)),
                    ))
            except Exception as e:
                logger.warning(f"LightRAG 查询失败: {e}")

        return results

    def drain_contradictions(self) -> list[dict]:
        """取出并清空待处理矛盾"""
        items = self.pending_contradictions
        self.pending_contradictions = []
        return items
