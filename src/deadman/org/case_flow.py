"""案件状态机（B2B-IMPLEMENTATION Step 5.3）

CASE_FLOW 定义合法状态迁移；每次状态变更必须落 case_events（审计）。
与 deadman_switch 的状态机模式一致：纯函数、无 IO、可单测。
"""

from __future__ import annotations

# 状态 → 允许迁往的状态集合
CASE_FLOW: dict[str, set[str]] = {
    "created": {"assigned", "in_progress", "cancelled"},
    "assigned": {"in_progress", "cancelled"},
    "in_progress": {"pending_input", "closed"},
    "pending_input": {"in_progress", "closed"},
    "closed": {"in_progress"},  # 重开
    "cancelled": set(),
}

# 终态：进入后不再受理推进（重开 closed 除外）
VALID_STATUSES = frozenset(CASE_FLOW.keys())


def can_transition(current: str | None, target: str) -> bool:
    """判断 current → target 是否合法；未知状态一律拒绝（安全兜底）。"""
    allowed = CASE_FLOW.get(current or "", set())
    return target in allowed


def validate_transition(current: str, target: str) -> list[str]:
    """返回状态迁移错误列表（空列表 = 合法）。

    覆盖两类错误：
      - current 不在状态机中（数据库脏数据兜底）
      - target 不在 allowed 集合（越权/乱序流转）
    """
    errors: list[str] = []
    if current not in VALID_STATUSES:
        errors.append(f"当前状态不在状态机中: {current}")
    if target not in VALID_STATUSES:
        errors.append(f"目标状态不在状态机中: {target}")
        return errors
    if current in VALID_STATUSES and target not in CASE_FLOW.get(current, set()):
        errors.append(f"状态迁移不合法: {current} → {target}")
    return errors
