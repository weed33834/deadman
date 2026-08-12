# 显式 Memory Store（分层记忆）

> 本文件定义智能体的显式分层记忆系统。借鉴 MemGPT（Berkeley, 2023）、Letta（MemGPT 生产版）、Mem0、Zep 2.0、LangChain Memory、OpenAI Assistants 的 Threads、A-MEM（Agentic Memory）。
>
> **目的**：把"上下文窗口"和"持久化记忆"分离。当前依赖 LLM 上下文窗口携带对话历史，存在三个问题：长对话截断丢失、跨会话不续接、无法选择性召回。显式 memory store 让智能体像人一样"分层记、按需忆"。

## 为什么需要分层记忆

### 当前痛点

```
用户第 1 轮：我爸 3 天前在北京去世了，我是独生子，母亲早逝
                ↓ 写入上下文窗口
用户第 5 轮：房产怎么过户？
                ↓ 上下文窗口可能已截断第 1 轮
智能体：请问您父亲在哪去世的？您是独生子女吗？
        ↑ 重复询问，用户体验差

用户隔天回来继续：
智能体：您好，请问有什么可以帮您？
        ↑ 完全不记得昨天的对话
```

### 分层记忆补强

```
1. Working Memory（工作记忆）—— 当前对话窗口，最近 N 轮
2. Episodic Memory（情景记忆）—— 过往对话片段，按时间索引
3. Semantic Memory（语义记忆）—— 用户画像、事实知识，结构化
4. Procedural Memory（程序记忆）—— 任务流程、操作步骤，技能化
```

## 四层记忆模型

### 1. Working Memory（工作记忆）

**类比**：人的短期记忆，当前正在想的事。

```python
# memory/working_memory.py（伪代码）

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class WorkingMemory:
    """工作记忆 - 当前对话上下文，最近 N 轮"""

    # 当前会话 ID
    session_id: str

    # 最近 N 轮对话（默认 10 轮）
    recent_turns: list[dict] = field(default_factory=list)
    # 每轮结构：
    # {
    #   "turn_id": "uuid",
    #   "role": "user" | "assistant",
    #   "content": "...",
    #   "timestamp": datetime,
    #   "agent": "death-aftercare",  # 哪个智能体回的
    #   "transfer_triggered": False,
    #   "subagent_called": ["death-aftercare-emotional"],
    # }

    # 当前活跃智能体
    current_agent: str = "death-aftercare"

    # 当前任务状态
    current_task: Optional[dict] = None
    # {
    #   "stage": 1,  # 9 阶段中的第几阶段
    #   "step_in_stage": "death_certificate",
    #   "pending_questions": ["父亲在哪去世的？"],
    #   "completed_items": ["确认关系", "确认地点"],
    # }

    # 待确认的转介
    pending_transfer: Optional[dict] = None

    # 本轮临时变量
    temp_vars: dict = field(default_factory=dict)
    # 如：{"knowledge_results": [...], "draft_response": "..."}

    MAX_TURNS = 10  # 可配置

    def add_turn(self, role, content, agent=None, **kwargs):
        """添加一轮对话"""
        self.recent_turns.append(
            {
                "turn_id": str(uuid4()),
                "role": role,
                "content": content,
                "timestamp": datetime.utcnow(),
                "agent": agent or self.current_agent,
                **kwargs,
            }
        )
        # 超过上限时，把最老的移到 episodic memory
        if len(self.recent_turns) > self.MAX_TURNS:
            old_turn = self.recent_turns.pop(0)
            episodic_memory.archive_turn(self.session_id, old_turn)

    def get_context_window(self) -> str:
        """生成给 LLM 的上下文窗口文本"""
        lines = []
        for turn in self.recent_turns:
            prefix = "用户" if turn["role"] == "user" else f"[{turn['agent']}]"
            lines.append(f"{prefix}: {turn['content']}")
        return "\n".join(lines)
```

### 2. Episodic Memory（情景记忆）

**类比**：人回忆"上次什么时候说了什么"。

```python
# memory/episodic_memory.py（伪代码）

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Episode:
    """一个情景片段"""

    episode_id: str
    session_id: str
    timestamp: datetime
    agent: str
    user_message: str
    assistant_response: str
    transfer_triggered: bool
    subagents_called: list[str]
    rule_check_result: Optional[dict]
    risk_tier: str
    summary: str  # 该片段的一句话摘要
    embedding: list[float]  # 用于语义检索


class EpisodicMemory:
    """情景记忆 - 过往对话片段，按时间 + 语义双索引"""

    def __init__(self, vector_store, graphiti_client):
        self.vector_store = vector_store  # 向量库（如 Chroma/Qdrant）
        self.graphiti = graphiti_client  # 与 Temporal-Memory-Graphiti.md 集成

    def archive_turn(self, session_id: str, turn: dict):
        """把工作记忆溢出的轮次归档到情景记忆"""
        # 1. 生成摘要
        summary = self._summarize_turn(turn)

        # 2. 生成 embedding
        embedding = self._embed(turn["content"])

        # 3. 存入向量库
        episode = Episode(
            episode_id=turn["turn_id"],
            session_id=session_id,
            timestamp=turn["timestamp"],
            agent=turn["agent"],
            user_message=turn["content"] if turn["role"] == "user" else "",
            assistant_response=turn["content"] if turn["role"] == "assistant" else "",
            transfer_triggered=turn.get("transfer_triggered", False),
            subagents_called=turn.get("subagent_called", []),
            rule_check_result=turn.get("rule_check_result"),
            risk_tier=turn.get("risk_tier", "R0"),
            summary=summary,
            embedding=embedding,
        )
        self.vector_store.add(episode)

        # 4. 同步到 Graphiti（时态记忆）
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

    def recall_by_time(self, session_id: str, start: datetime, end: datetime) -> list[Episode]:
        """按时间范围回忆"""
        return self.vector_store.query(
            filter={"session_id": session_id, "timestamp": {"$gte": start, "$lte": end}}
        )

    def recall_by_semantic(self, query: str, top_k: int = 5) -> list[Episode]:
        """按语义相似度回忆"""
        query_embedding = self._embed(query)
        return self.vector_store.search(query_embedding, top_k=top_k)

    def recall_recent(self, session_id: str, n: int = 5) -> list[Episode]:
        """回忆最近 N 个片段"""
        return self.vector_store.query(
            filter={"session_id": session_id},
            sort_by="timestamp",
            sort_order="desc",
            limit=n,
        )

    def _summarize_turn(self, turn: dict) -> str:
        """用 LLM 生成片段摘要"""
        prompt = f"""
        用一句话总结以下对话片段的核心信息：
        角色：{turn["role"]}
        内容：{turn["content"]}
        智能体：{turn.get("agent", "unknown")}

        摘要要求：
        - 包含关键事实（人物/时间/地点/事件）
        - 不超过 50 字
        - 用于后续检索
        """
        return call_llm(prompt)

    def _embed(self, text: str) -> list[float]:
        """生成 embedding"""
        return embedding_model.encode(text)
```

### 3. Semantic Memory（语义记忆）

**类比**：人的知识库，"我爸叫张三，是北京人"这类事实。

```python
# memory/semantic_memory.py（伪代码）

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UserProfile:
    """用户画像 - 语义记忆的核心"""

    user_id: str
    name: Optional[str] = None
    relationship_to_deceased: Optional[str] = None  # 子女/配偶/父母/...
    location: Optional[dict] = None  # {country, region, city}
    deceased_info: Optional[dict] = None
    # {
    #   "name": "张三",
    #   "death_date": "2026-07-10",
    #   "death_location": {"country": "CN", "city": "北京"},
    #   "cause": "病逝",
    #   "nationality": "CN",
    #   "domicile": "北京",
    # }
    family_structure: Optional[dict] = None
    # {
    #   "spouse": {"alive": True, "name": "李四"},
    #   "children": [{"name": "用户本人", "is_only_child": True}],
    #   "parents": [{"alive": False, "name": "张三"}],
    # }
    assets_summary: Optional[dict] = None
    # {
    #   "real_estate_count": 2,
    #   "has_bank_accounts": True,
    #   "has_will": False,
    #   "estimated_estate_value": None,  # 不存储具体金额
    # }
    current_stage: Optional[int] = None  # 9 阶段中的第几阶段
    completed_stages: list[int] = field(default_factory=list)
    pending_tasks: list[str] = field(default_factory=list)


@dataclass
class Fact:
    """事实知识 - 语义记忆的单元"""

    fact_id: str
    fact_type: str  # user_profile / policy / procedure / entity
    content: str
    source: str  # 来源（用户告知 / 知识库检索 / 政策搜索）
    confidence: float  # 0.0-1.0
    jurisdiction: Optional[dict] = None
    valid_time: Optional[dict] = None  # 与 Graphiti 集成
    verified: bool = False  # 是否经过 integrity check
    supersedes: Optional[str] = None  # 取代了哪条旧事实


class SemanticMemory:
    """语义记忆 - 结构化事实知识"""

    def __init__(self, graphiti_client, lightrag_client):
        self.graphiti = graphiti_client
        self.lightrag = lightrag_client
        self.user_profiles = {}  # user_id → UserProfile
        self.facts = {}  # fact_id → Fact

    def update_user_profile(self, user_id: str, updates: dict):
        """更新用户画像"""
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = UserProfile(user_id=user_id)

        profile = self.user_profiles[user_id]
        for key, value in updates.items():
            if hasattr(profile, key):
                # 检测前后矛盾（integrity-framework）
                old_value = getattr(profile, key)
                if old_value is not None and old_value != value:
                    # 触发矛盾处理
                    self._handle_contradiction(user_id, key, old_value, value)
                setattr(profile, key, value)

        # 同步到 Graphiti（时态记忆，保留历史版本）
        self.graphiti.add_event(
            {
                "event_type": "UserProgressEvent",
                "user_id": user_id,
                "profile_update": updates,
                "timestamp": datetime.utcnow(),
            }
        )

    def _handle_contradiction(self, user_id, field, old_value, new_value):
        """
        用户前后矛盾检测 - integrity-framework 第三章
        立刻提出，不猜测
        """
        # 记录矛盾
        contradiction = {
            "user_id": user_id,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "detected_at": datetime.utcnow(),
        }
        # 注入到 working memory，让智能体在下一轮提出
        working_memory.add_contradiction_alert(contradiction)

    def add_fact(self, fact: Fact):
        """添加事实知识"""
        # 若有旧版本被取代，标记
        if fact.supersedes and fact.supersedes in self.facts:
            old_fact = self.facts[fact.supersedes]
            old_fact.valid_time = {
                "valid_from": old_fact.valid_time["valid_from"],
                "valid_to": datetime.utcnow(),
            }

        self.facts[fact.fact_id] = fact

        # 同步到 Graphiti（PolicyFact 类型，时态管理）
        self.graphiti.add_event(
            {
                "event_type": "PolicyFact" if fact.fact_type == "policy" else "KnowledgeVersion",
                "fact_id": fact.fact_id,
                "content": fact.content,
                "source": fact.source,
                "confidence": fact.confidence,
                "valid_time": fact.valid_time,
                "supersedes": fact.supersedes,
                "transaction_time": datetime.utcnow(),
            }
        )

    def query_facts(self, query: str, jurisdiction=None, fact_type=None) -> list[Fact]:
        """查询事实知识"""
        results = []

        # 1. 先在本地 facts 中精确匹配
        for fact in self.facts.values():
            if fact_type and fact.fact_type != fact_type:
                continue
            if jurisdiction and fact.jurisdiction != jurisdiction:
                continue
            if query.lower() in fact.content.lower():
                results.append(fact)

        # 2. 若本地不足，调用 LightRAG 知识图谱
        if len(results) < 3:
            graph_results = self.lightrag.query(query, mode="hybrid")
            for r in graph_results:
                results.append(
                    Fact(
                        fact_id=str(uuid4()),
                        fact_type="entity",
                        content=r["content"],
                        source=r.get("source", "lightrag"),
                        confidence=r.get("confidence", 0.7),
                    )
                )

        return results
```

### 4. Procedural Memory（程序记忆）

**类比**：人的技能记忆，"办死亡证明要先去医院，带身份证"。

```python
# memory/procedural_memory.py（伪代码）

from dataclasses import dataclass, field


@dataclass
class Procedure:
    """程序记忆 - 任务流程"""

    procedure_id: str
    procedure_name: str
    jurisdiction: dict  # 适用地域
    steps: list[dict] = field(default_factory=list)
    # [
    #   {
    #     "step_id": 1,
    #     "action": "前往医院开具死亡证明",
    #     "required_documents": ["逝者身份证", "申请人身份证", "亲属关系证明"],
    #     "responsible_authority": "医院",
    #     "time_estimate": "1-2 小时",
    #     "common_issues": ["非正常死亡需法医", "异地死亡需特殊处理"],
    #     "next_step": 2,
    #   },
    #   ...
    # ]
    required_documents_total: list[str] = field(default_factory=list)
    estimated_total_time: Optional[str] = None
    source: str = "knowledge_base"  # knowledge_base / web_search / user_taught
    verified: bool = False
    last_updated: Optional[datetime] = None


@dataclass
class UserProgress:
    """用户在某流程上的进度"""

    user_id: str
    procedure_id: str
    current_step: int
    completed_steps: list[int]
    skipped_steps: list[int]
    started_at: datetime
    last_active_at: datetime
    notes: dict  # 用户自己记录的备注


class ProceduralMemory:
    """程序记忆 - 流程知识 + 用户进度"""

    def __init__(self, graphiti_client):
        self.graphiti = graphiti_client
        self.procedures = {}  # procedure_id → Procedure
        self.user_progress = {}  # (user_id, procedure_id) → UserProgress

    def get_procedure(self, procedure_name: str, jurisdiction: dict) -> Optional[Procedure]:
        """获取流程"""
        for proc in self.procedures.values():
            if proc.procedure_name == procedure_name:
                if self._jurisdiction_matches(proc.jurisdiction, jurisdiction):
                    return proc
        return None

    def get_user_progress(self, user_id: str, procedure_id: str) -> Optional[UserProgress]:
        """获取用户进度"""
        return self.user_progress.get((user_id, procedure_id))

    def update_user_progress(self, user_id: str, procedure_id: str, step_completed: int):
        """更新用户进度"""
        key = (user_id, procedure_id)
        if key not in self.user_progress:
            self.user_progress[key] = UserProgress(
                user_id=user_id,
                procedure_id=procedure_id,
                current_step=1,
                completed_steps=[],
                skipped_steps=[],
                started_at=datetime.utcnow(),
                last_active_at=datetime.utcnow(),
                notes={},
            )

        progress = self.user_progress[key]
        if step_completed not in progress.completed_steps:
            progress.completed_steps.append(step_completed)
        progress.current_step = step_completed + 1
        progress.last_active_at = datetime.utcnow()

        # 同步到 Graphiti（跨会话续接）
        self.graphiti.add_event(
            {
                "event_type": "UserProgressEvent",
                "user_id": user_id,
                "procedure_id": procedure_id,
                "completed_steps": progress.completed_steps,
                "current_step": progress.current_step,
                "timestamp": datetime.utcnow(),
            }
        )

    def learn_from_user(
        self, user_id: str, procedure_name: str, jurisdiction: dict, user_correction: dict
    ):
        """
        从用户反馈中学习 - 用户纠正了流程的某一步
        与 Reflexion 机制集成
        """
        proc = self.get_procedure(procedure_name, jurisdiction)
        if not proc:
            return

        # 根据用户反馈修改流程
        step_id = user_correction["step_id"]
        for step in proc.steps:
            if step["step_id"] == step_id:
                step["user_correction"] = user_correction["correction"]
                step["corrected_by_user"] = user_id
                step["corrected_at"] = datetime.utcnow()
                break

        proc.last_updated = datetime.utcnow()
        proc.verified = False  # 需要重新验证

        # 记录到 Graphiti 作为 KnowledgeVersion
        self.graphiti.add_event(
            {
                "event_type": "KnowledgeVersion",
                "procedure_id": proc.procedure_id,
                "change": "user_correction",
                "user_correction": user_correction,
                "transaction_time": datetime.utcnow(),
            }
        )
```

## Memory Manager（统一管理）

```python
# memory/memory_manager.py（伪代码）

from memory import WorkingMemory, EpisodicMemory, SemanticMemory, ProceduralMemory


class MemoryManager:
    """统一记忆管理 - 对接 LangGraph state"""

    def __init__(self, vector_store, graphiti, lightrag):
        self.working = WorkingMemory(session_id="")
        self.episodic = EpisodicMemory(vector_store, graphiti)
        self.semantic = SemanticMemory(graphiti, lightrag)
        self.procedural = ProceduralMemory(graphiti)

    def start_session(self, user_id: str, session_id: str):
        """开始新会话 - 恢复历史记忆"""
        self.working.session_id = session_id

        # 1. 恢复用户画像（语义记忆）
        profile = self.semantic.user_profiles.get(user_id)
        if profile:
            # 注入到工作记忆的 temp_vars
            self.working.temp_vars["user_profile"] = profile

        # 2. 恢复最近情景（情景记忆）
        recent_episodes = self.episodic.recall_recent(session_id, n=3)
        if recent_episodes:
            self.working.temp_vars["recent_episodes_summary"] = [e.summary for e in recent_episodes]

        # 3. 恢复流程进度（程序记忆）
        if profile and profile.current_stage:
            # 查找用户当前阶段的流程
            procedures = self.procedural.get_procedure_by_stage(profile.current_stage)
            for proc in procedures:
                progress = self.procedural.get_user_progress(user_id, proc.procedure_id)
                if progress:
                    self.working.temp_vars["resumed_progress"] = {
                        "procedure": proc.procedure_name,
                        "current_step": progress.current_step,
                        "completed_steps": progress.completed_steps,
                    }

    def build_context_for_llm(self, user_input: str) -> str:
        """
        为 LLM 构建完整的上下文。
        不是简单塞入全部历史，而是选择性召回。
        """
        context_parts = []

        # 1. 工作记忆（最近 N 轮）
        context_parts.append("=== 最近对话 ===")
        context_parts.append(self.working.get_context_window())

        # 2. 语义召回（与当前输入相关的历史片段）
        relevant_episodes = self.episodic.recall_by_semantic(user_input, top_k=3)
        if relevant_episodes:
            context_parts.append("\n=== 相关历史 ===")
            for ep in relevant_episodes:
                context_parts.append(f"[{ep.timestamp}] {ep.summary}")

        # 3. 用户画像
        profile = self.working.temp_vars.get("user_profile")
        if profile:
            context_parts.append("\n=== 用户画像 ===")
            context_parts.append(self._format_profile(profile))

        # 4. 当前流程进度
        progress = self.working.temp_vars.get("resumed_progress")
        if progress:
            context_parts.append("\n=== 当前进度 ===")
            context_parts.append(f"流程：{progress['procedure']}")
            context_parts.append(f"当前步骤：第 {progress['current_step']} 步")
            context_parts.append(f"已完成：{progress['completed_steps']}")

        # 5. 待处理的矛盾
        contradictions = self.working.temp_vars.get("pending_contradictions", [])
        if contradictions:
            context_parts.append("\n=== 待澄清的矛盾 ===")
            for c in contradictions:
                context_parts.append(
                    f"用户之前说 {c['field']}={c['old_value']}，现在说 {c['new_value']}，需要澄清"
                )

        return "\n".join(context_parts)

    def after_turn(
        self, user_id: str, user_input: str, assistant_response: str, agent: str, **kwargs
    ):
        """一轮对话结束后，更新各层记忆"""
        # 1. 写入工作记忆
        self.working.add_turn("user", user_input)
        self.working.add_turn("assistant", assistant_response, agent=agent, **kwargs)

        # 2. 提取事实，更新语义记忆
        facts = self._extract_facts(user_input, assistant_response)
        for fact in facts:
            self.semantic.update_user_profile(user_id, fact)

        # 3. 更新流程进度
        if kwargs.get("step_completed"):
            procedure_id = kwargs.get("procedure_id")
            self.procedural.update_user_progress(user_id, procedure_id, kwargs["step_completed"])

    def _extract_facts(self, user_input, assistant_response):
        """从对话中提取事实"""
        prompt = f"""
        从以下对话中提取用户的事实信息：
        用户：{user_input}
        智能体：{assistant_response}

        输出 JSON，只包含明确提到的事实，不要猜测：
        {{
          "location": {{"country": "...", "city": "..."}},
          "relationship_to_deceased": "...",
          "deceased_info": {{...}},
          "family_structure": {{...}}
        }}

        若某字段在对话中未明确提及，设为 null。
        """
        return call_llm(prompt)

    def _format_profile(self, profile):
        """格式化用户画像"""
        lines = []
        if profile.relationship_to_deceased:
            lines.append(f"关系：{profile.relationship_to_deceased}")
        if profile.location:
            lines.append(f"地点：{profile.location.get('city', '')}")
        if profile.deceased_info:
            d = profile.deceased_info
            lines.append(f"逝者：{d.get('name', '未知')}, 去世日期：{d.get('death_date', '未知')}")
        if profile.current_stage:
            lines.append(f"当前阶段：第 {profile.current_stage} 阶段")
        return "\n".join(lines)
```

## 与 LangGraph 的集成

```python
# memory/langgraph_integration.py


def inject_memory_to_state(state: ConversationState, memory_manager: MemoryManager):
    """把记忆注入到 LangGraph state"""
    state["context_for_llm"] = memory_manager.build_context_for_llm(state["user_input"])
    state["user_profile"] = memory_manager.working.temp_vars.get("user_profile")
    state["resumed_progress"] = memory_manager.working.temp_vars.get("resumed_progress")
    state["pending_contradictions"] = memory_manager.working.temp_vars.get(
        "pending_contradictions", []
    )
    return state


def update_memory_after_turn(state: ConversationState, memory_manager: MemoryManager):
    """一轮结束后更新记忆"""
    memory_manager.after_turn(
        user_id=state["user_id"],
        user_input=state["user_input"],
        assistant_response=state["final_response"],
        agent=state["current_agent"],
        step_completed=state.get("step_completed"),
        procedure_id=state.get("procedure_id"),
        transfer_triggered=bool(state.get("pending_transfer")),
        subagents_called=[r.subagent_name for r in state.get("subagent_results", [])],
        rule_check_result=state.get("rule_check"),
        risk_tier=state.get("rule_check", {}).risk_tier if state.get("rule_check") else "R0",
    )
```

## 数据保留策略（与 Graphiti 对齐）

| 记忆层 | 保留期 | 存储位置 | 说明 |
|--------|--------|---------|------|
| Working Memory | 当前会话 | 内存 | 会话结束归档到 Episodic |
| Episodic Memory | 7 年 | 向量库 + Graphiti | 与 PIPL 一致 |
| Semantic Memory - 用户画像 | 7 年 | Graphiti | 用户可请求删除 |
| Semantic Memory - 政策事实 | 永久 | Graphiti | 历史价值 |
| Procedural Memory - 流程 | 永久 | Graphiti | 持续更新 |
| Procedural Memory - 用户进度 | 7 年 | Graphiti | 与用户画像同步 |

## 隐私与合规

```python
# memory/privacy.py


class PrivacyManager:
    """隐私管理 - 与 compliance-framework 集成"""

    PII_FIELDS = {"identifier", "name", "address", "phone", "account_number"}

    def sanitize_before_store(self, data: dict) -> dict:
        """存储前脱敏"""
        sanitized = {}
        for key, value in data.items():
            if key in self.PII_FIELDS:
                sanitized[key] = self._mask_pii(value)
            else:
                sanitized[key] = value
        return sanitized

    def _mask_pii(self, value):
        """脱敏处理"""
        if isinstance(value, str) and len(value) > 4:
            return value[:2] + "***" + value[-2:]
        return "***"

    def handle_deletion_request(self, user_id: str):
        """用户请求删除（GDPR/PIPL）"""
        # 删除语义记忆中的用户画像
        # 删除情景记忆中该用户的所有片段
        # 删除程序记忆中的用户进度
        # Graphiti 中标记为 deleted（保留审计痕迹）
        pass
```

## 评估指标

| 指标 | 目标 | 说明 |
|------|------|------|
| 跨会话续接成功率 | ≥ 0.95 | 用户隔天回来能正确恢复进度 |
| 重复询问率 | ≤ 0.05 | 已告知信息不再重复问 |
| 上下文召回准确率 | ≥ 0.90 | 语义召回的相关性 |
| 矛盾检测率 | 1.0 | 用户前后矛盾 100% 检测 |
| 记忆查询延迟 P95 | ≤ 200ms | 不影响主流程 |
| PII 脱敏率 | 1.0 | 所有 PII 字段存储前脱敏 |

## 版本

- v1.0 初始分层记忆方案（4 层模型 + Memory Manager + LangGraph 集成 + Graphiti 同步 + 隐私合规）
```
