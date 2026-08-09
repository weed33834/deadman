"""P4.2 Scratchpad - agent 私有/共享草稿本

借鉴 OpenAI Swarm / AutoGen 的 scratchpad 设计，让每个 agent 维护一个
轻量"草稿本"，记录中间推理、待办事项、跨步骤备忘。Handoff 时可显式共享
给目标 agent，避免上下文丢失。

核心组件：
- ScratchpadManager: 管理多 agent 的草稿本，支持两种模式
  - "independent"（默认）: 每 agent 独立 scratchpad，handoff 时显式 share_to
  - "shared": 所有 agent 读写同一 scratchpad（适合紧耦合协作）

Feature flag: DEADMAN_SCRATCHPAD_ENABLED=0 默认关闭
- 关闭时所有写操作（add/clear/share_to）静默 no-op，读操作返回空列表，
  调用方（agent_node 等）感知不到任何变化，行为完全不变
- 开启时所有操作生效；scratchpads 数据存储在 ConversationState["scratchpads"]
  字典中，由 LangGraph 的 checkpointer 自动持久化

降级路径全覆盖：
1. feature flag 关闭 → 所有写操作 no-op，读操作返回 []
2. agent_name 不存在 → 自动创建空 scratchpad（懒初始化）
3. mode 切换 → 仅影响后续 share_to 行为，已有数据不丢
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认关闭
# =====================================================================
SCRATCHPAD_ENABLED: bool = os.environ.get("DEADMAN_SCRATCHPAD_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# 支持的模式
VALID_MODES = ("independent", "shared")
DEFAULT_MODE = "independent"


class ScratchpadManager:
    """Scratchpad 管理器 - 多 agent 草稿本的增删改查

    所有方法在 SCRATCHPAD_ENABLED=False 时静默 no-op（保证向后兼容）。

    Usage:
        mgr = ScratchpadManager()
        mgr.add("legal_advisor", "用户提到有海外资产")
        notes = mgr.get("legal_advisor")  # ["用户提到有海外资产"]
        mgr.share_to("cross_border_specialist", "legal_advisor")
    """

    def __init__(
        self,
        mode: str = DEFAULT_MODE,
        state: dict[str, Any] | None = None,
    ):
        """Args:
        mode: "independent"（默认）或 "shared"
        state: 可选的 ConversationState；若提供则 scratchpads 读写都
               落到 state["scratchpads"]，便于 LangGraph 持久化；
               为 None 时内部维护独立 dict
        """
        if mode not in VALID_MODES:
            logger.warning("未知 scratchpad mode=%s，回退到 %s", mode, DEFAULT_MODE)
            mode = DEFAULT_MODE
        self.mode = mode
        self._state = state
        # state 为 None 时用内部 dict（不影响外部 state）
        self._internal: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # 内部存储访问
    # ------------------------------------------------------------------

    def _store(self) -> dict[str, list[str]]:
        """获取 scratchpads 存储引用（state 优先）"""
        if self._state is not None:
            store = self._state.get("scratchpads")
            if not isinstance(store, dict):
                store = {}
                self._state["scratchpads"] = store
            return store
        return self._internal

    def _shared_key(self) -> str:
        """shared 模式下所有 agent 共用的 key"""
        return "__shared__"

    def _resolve_key(self, agent_name: str) -> str:
        """根据 mode 解析实际存储 key

        - independent → agent_name 本身
        - shared → 固定 "__shared__"（所有 agent 读写同一列表）
        """
        if self.mode == "shared":
            return self._shared_key()
        return agent_name

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def add(self, agent_name: str, note: str) -> None:
        """追加一条 note 到 agent 的 scratchpad

        feature flag 关闭时静默 no-op。
        """
        if not SCRATCHPAD_ENABLED:
            return
        if not agent_name or not note:
            return
        store = self._store()
        key = self._resolve_key(agent_name)
        notes = store.get(key, [])
        notes.append(str(note))
        store[key] = notes
        logger.debug("scratchpad add [%s]: %s", key, note[:80])

    def get(self, agent_name: str) -> list[str]:
        """读取 agent 的 scratchpad（返回副本，避免外部修改）

        feature flag 关闭时返回 []（保持旧的无 scratchpad 行为）。
        """
        if not SCRATCHPAD_ENABLED:
            return []
        store = self._store()
        key = self._resolve_key(agent_name)
        return list(store.get(key, []))

    def clear(self, agent_name: str) -> None:
        """清空 agent 的 scratchpad

        feature flag 关闭时静默 no-op。
        """
        if not SCRATCHPAD_ENABLED:
            return
        store = self._store()
        key = self._resolve_key(agent_name)
        if key in store:
            store[key] = []
            logger.debug("scratchpad cleared [%s]", key)

    def share_to(self, target_agent: str, source_agent: str) -> None:
        """共享模式：复制源 agent 的 scratchpad 到目标 agent

        仅在 independent 模式下有意义（shared 模式所有 agent 已共享同一 scratchpad）。
        - independent: 把 source 的 notes 追加到 target 的 notes 末尾
        - shared: no-op（已共享）

        feature flag 关闭时静默 no-op。

        Args:
            target_agent: 接收 scratchpad 的目标 agent
            source_agent: 提供 scratchpad 的源 agent
        """
        if not SCRATCHPAD_ENABLED:
            return
        if self.mode == "shared":
            # 已共享，无需复制
            return
        store = self._store()
        source_notes = list(store.get(source_agent, []))
        if not source_notes:
            return
        target_notes = store.get(target_agent, [])
        target_notes.extend(source_notes)
        store[target_agent] = target_notes
        logger.debug(
            "scratchpad shared: %s -> %s (%d notes)",
            source_agent,
            target_agent,
            len(source_notes),
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def list_agents(self) -> list[str]:
        """列出所有有 scratchpad 的 agent 名（排除 __shared__ 内部 key）"""
        store = self._store()
        return [k for k in store if k != self._shared_key()]

    def count(self, agent_name: str) -> int:
        """返回 agent 的 scratchpad 条数（feature flag 关闭返回 0）"""
        if not SCRATCHPAD_ENABLED:
            return 0
        return len(self.get(agent_name))
