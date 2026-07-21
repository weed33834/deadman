"""工作记忆 - 当前对话上下文，最近 N 轮。

类比人的短期记忆：当前正在想的事。超过 MAX_TURNS 时，最老的轮次归档到情景记忆。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, Optional
from uuid import uuid4

from ..config import settings

if TYPE_CHECKING:
    from .episodic import EpisodicMemory

logger = logging.getLogger(__name__)


class WorkingMemory:
    """工作记忆 - 当前对话窗口，最近 N 轮。

    每轮结构：
        {
            "turn_id": "uuid",
            "role": "user" | "assistant",
            "content": "...",
            "timestamp": datetime,
            "agent": "death-aftercare",
            "transfer_triggered": False,
            "subagent_called": [...],
        }
    """

    def __init__(
        self,
        session_id: str = "",
        current_agent: str = "death-aftercare",
        max_turns: int | None = None,
    ):
        self.session_id = session_id
        self.recent_turns: list[dict[str, Any]] = []
        self.current_agent = current_agent
        self.current_task: Optional[dict] = None
        self.pending_transfer: Optional[dict] = None
        self.temp_vars: dict[str, Any] = {}
        # 情景记忆引用，由 MemoryManager 注入；溢出时调用其 archive_turn
        self._episodic: Optional[EpisodicMemory] = None
        # 最大保留轮次（默认取配置）
        self.MAX_TURNS = max_turns if max_turns is not None else settings.memory_max_turns

    def set_episodic(self, episodic: EpisodicMemory) -> None:
        """注入情景记忆引用，用于溢出归档"""
        self._episodic = episodic

    async def add_turn(
        self,
        role: str,
        content: str,
        agent: str | None = None,
        **kwargs: Any,
    ) -> None:
        """添加一轮对话。

        超过 MAX_TURNS 时把最老的归档到情景记忆（若已注入）。
        """
        turn: dict[str, Any] = {
            "turn_id": str(uuid4()),
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc),
            "agent": agent or self.current_agent,
            **kwargs,
        }
        self.recent_turns.append(turn)

        # 超过上限，把最老的归档到情景记忆
        while len(self.recent_turns) > self.MAX_TURNS:
            old_turn = self.recent_turns.pop(0)
            if self._episodic is not None:
                try:
                    await self._episodic.archive_turn(self.session_id, old_turn)
                except Exception as e:
                    logger.warning(f"归档到情景记忆失败: {e}")

    def get_context_window(self) -> str:
        """生成给 LLM 的上下文窗口文本"""
        lines: list[str] = []
        for turn in self.recent_turns:
            if turn["role"] == "user":
                prefix = "用户"
            else:
                prefix = f"[{turn.get('agent', 'assistant')}]"
            lines.append(f"{prefix}: {turn['content']}")
        return "\n".join(lines)

    def add_contradiction_alert(self, contradiction: dict) -> None:
        """注入待澄清的矛盾告警（由 SemanticMemory 调用）"""
        self.temp_vars.setdefault("pending_contradictions", []).append(contradiction)

    def clear_contradictions(self) -> None:
        """清空已处理的矛盾"""
        self.temp_vars["pending_contradictions"] = []
