"""分层记忆系统 - 4 层模型：工作 / 情景 / 语义 / 程序。

参考 Memory-Store.md 设计：
    1. WorkingMemory   - 当前对话窗口，最近 N 轮
    2. EpisodicMemory  - 过往对话片段，按时间 + 语义双索引
    3. SemanticMemory  - 用户画像 + 事实知识，结构化
    4. ProceduralMemory- 任务流程 + 操作步骤，技能化

MemoryManager 统一管理 4 层记忆，提供 start_session / build_context_for_llm /
after_turn 三个核心入口。

P2 增强层(feature flag 默认关闭):
    - vector_store: Chroma/InMemory 向量库(DEADMAN_VECTOR_STORE_ENABLED)
    - shared_knowledge: 跨用户匿名知识共享(DEADMAN_SHARED_KNOWLEDGE_ENABLED)
"""

from __future__ import annotations

from .working import WorkingMemory
from .episodic import EpisodicMemory, Episode
from .semantic import SemanticMemory, UserProfile, Fact
from .procedural import ProceduralMemory, Procedure, UserProgress
from .manager import MemoryManager, sanitize_before_store

__all__ = [
    "WorkingMemory",
    "EpisodicMemory",
    "Episode",
    "SemanticMemory",
    "UserProfile",
    "Fact",
    "ProceduralMemory",
    "Procedure",
    "UserProgress",
    "MemoryManager",
    "sanitize_before_store",
]
