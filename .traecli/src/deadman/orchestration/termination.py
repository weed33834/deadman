"""可组合终止条件 - 借鉴 AutoGen TerminationCondition 设计

AutoGen 的核心思想：终止条件是可组合对象，用 `|` (OR) 和 `&` (AND) 拼装，
任一/全部满足时终止。deadman 在此基础上做了三项简化：

1. 评估对象用 ConversationState 而非消息序列（更通用，可读 step_count/metrics 等）
2. 条件对象无状态（无 AutoGen 的 terminated/reset 状态机），每轮新 state 天然隔离
3. token usage 走 state["metrics"]["token_usage"]，不引入第三个累积器

向后兼容：default_termination() 等价于现有 P4 的 MAX_STEPS + STUCK_AGENT_REPEAT_LIMIT。

使用示例：

    from deadman.orchestration.termination import (
        default_termination, MaxStepsTermination, TokenUsageTermination,
    )

    # 默认（等价 P4）
    term = default_termination()

    # 默认 + token 上限
    term = default_termination() | TokenUsageTermination(token_limit=50_000)

    # 步数和 token 都爆才终止（AND）
    term = MaxStepsTermination(50) & TokenUsageTermination(100_000)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .state import ConversationState


# =====================================================================
# 核心抽象
# =====================================================================


@dataclass(frozen=True)
class TerminationResult:
    """终止评估结果（不可变，利于断言）

    Attributes:
        should_terminate: 是否应终止
        reason: 终止原因（人类可读，如 "max_steps:26>25"），终止时非空
        source: 触发的条件类名（如 "MaxStepsTermination"），终止时非空
    """

    should_terminate: bool
    reason: str = ""
    source: str = ""


class TerminationCondition(ABC):
    """终止条件抽象基类 - 同步纯函数，无内部可变状态

    子类只需实现 evaluate()。`|` 和 `&` 操作符已在本基类实现，
    返回 _OrTerminationCondition / _AndTerminationCondition 组合对象。
    """

    @abstractmethod
    def evaluate(self, state: ConversationState) -> TerminationResult:
        """评估是否应终止。纯函数：相同 state 必须返回相同结果。"""

    def __or__(self, other: "TerminationCondition") -> "TerminationCondition":
        """OR 组合：任一终止即终止（短路：左侧终止时不评估右侧）"""
        return _OrTerminationCondition(self, other)

    def __and__(self, other: "TerminationCondition") -> "TerminationCondition":
        """AND 组合：两者都终止才终止（收集两侧 reason）"""
        return _AndTerminationCondition(self, other)


# =====================================================================
# 组合条件
# =====================================================================


class _OrTerminationCondition(TerminationCondition):
    """A | B：A 或 B 任一终止即终止（短路）"""

    def __init__(
        self, left: TerminationCondition, right: TerminationCondition
    ) -> None:
        self._left = left
        self._right = right

    def evaluate(self, state: ConversationState) -> TerminationResult:
        r1 = self._left.evaluate(state)
        if r1.should_terminate:
            return r1
        return self._right.evaluate(state)

    def __repr__(self) -> str:
        return f"({self._left!r} | {self._right!r})"


class _AndTerminationCondition(TerminationCondition):
    """A & B：A 和 B 都终止才终止"""

    def __init__(
        self, left: TerminationCondition, right: TerminationCondition
    ) -> None:
        self._left = left
        self._right = right

    def evaluate(self, state: ConversationState) -> TerminationResult:
        r1 = self._left.evaluate(state)
        r2 = self._right.evaluate(state)
        if r1.should_terminate and r2.should_terminate:
            return TerminationResult(
                True,
                f"({r1.reason}) AND ({r2.reason})",
                "And",
            )
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"({self._left!r} & {self._right!r})"


# =====================================================================
# 具体终止条件
# =====================================================================


class MaxStepsTermination(TerminationCondition):
    """节点执行步数超限即终止（对应 AutoGen MaxMessageTermination）

    借鉴 AutoGen MaxMessageTermination：消息数超限即终止。
    deadman 用 step_count（节点执行数）近似，因 deadman 单轮对话的"消息"
    等价于"经过的节点数"。
    """

    def __init__(self, max_steps: int = 25) -> None:
        self.max_steps = max_steps

    def evaluate(self, state: ConversationState) -> TerminationResult:
        n = int(state.get("step_count", 0))
        if n > self.max_steps:
            return TerminationResult(
                True,
                f"max_steps:{n}>{self.max_steps}",
                "MaxStepsTermination",
            )
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"MaxStepsTermination({self.max_steps})"


class StuckAgentTermination(TerminationCondition):
    """连续路由到同一 agent 超限即终止（deadman 特有，OpenManus 风格）

    借鉴 OpenManus BaseAgent.is_stuck：连续多次路由到同一 agent 判定为卡死。
    """

    def __init__(self, repeat_limit: int = 3) -> None:
        self.repeat_limit = repeat_limit

    def evaluate(self, state: ConversationState) -> TerminationResult:
        c = int(state.get("stuck_count", 0))
        if c >= self.repeat_limit:
            last = str(state.get("last_agent_for_stuck", ""))
            return TerminationResult(
                True,
                f"agent_stuck:{last}:{c}_repeats",
                "StuckAgentTermination",
            )
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"StuckAgentTermination({self.repeat_limit})"


class TokenUsageTermination(TerminationCondition):
    """本轮累计 token 超限即终止（对应 AutoGen TokenUsageTermination）

    借鉴 AutoGen TokenUsageTermination：累计 token 超阈值即终止。
    deadman 从 state["metrics"]["token_usage"] 读取，避免 cost_tracker
    跨会话串扰。

    Args:
        token_limit: token 上限
        field: 比较字段，"total_tokens" / "prompt_tokens" / "completion_tokens"
    """

    def __init__(
        self, token_limit: int, field: str = "total_tokens"
    ) -> None:
        self.token_limit = token_limit
        self.field = field

    def evaluate(self, state: ConversationState) -> TerminationResult:
        metrics: dict[str, Any] = state.get("metrics", {}) or {}
        usage = metrics.get("token_usage", {}) or {}
        used = int(usage.get(self.field, 0))
        if used > self.token_limit:
            return TerminationResult(
                True,
                f"token_usage:{self.field}:{used}>{self.token_limit}",
                "TokenUsageTermination",
            )
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"TokenUsageTermination({self.token_limit}, {self.field!r})"


class MessageCountTermination(TerminationCondition):
    """本轮 agent 调用次数超限即终止（对应 AutoGen MaxMessageTermination）

    基于 agent_history 长度（每次 agent_node 执行追加一条）。
    """

    def __init__(self, max_messages: int) -> None:
        self.max_messages = max_messages

    def evaluate(self, state: ConversationState) -> TerminationResult:
        history = state.get("agent_history", []) or []
        n = len(history)
        if n >= self.max_messages:
            return TerminationResult(
                True,
                f"message_count:{n}>={self.max_messages}",
                "MessageCountTermination",
            )
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"MessageCountTermination({self.max_messages})"


class ExternalTermination(TerminationCondition):
    """外部手动 set() 触发的终止（对应 AutoGen ExternalTermination）

    借鉴 AutoGen ExternalTermination：外部信号触发终止。
    用例：用户主动点"停止"按钮 / 上游超时 / 运维干预。
    """

    def __init__(self) -> None:
        self._flag = False

    def set(self) -> None:
        """外部触发终止"""
        self._flag = True

    def reset(self) -> None:
        """重置标志（仅 ExternalTermination 保留 reset，因它本身有状态）"""
        self._flag = False

    def evaluate(self, state: ConversationState) -> TerminationResult:
        if self._flag:
            return TerminationResult(True, "external_triggered", "ExternalTermination")
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"ExternalTermination(flag={self._flag})"


class TextMentionTermination(TerminationCondition):
    """指定 state 字段含关键词即终止（对应 AutoGen TextMessageTermination）

    借鉴 AutoGen TextMessageTermination：消息含指定文本即终止。
    用例：用户输入"停止"/"结束对话"时主动终止。

    Args:
        keyword: 触发关键词
        source_field: 读取的 state 字段，默认 "user_input"
    """

    def __init__(self, keyword: str, source_field: str = "user_input") -> None:
        self.keyword = keyword
        self.source_field = source_field

    def evaluate(self, state: ConversationState) -> TerminationResult:
        text = str(state.get(self.source_field, "") or "")
        if self.keyword in text:
            return TerminationResult(
                True,
                f"text_mention:{self.keyword}@{self.source_field}",
                "TextMentionTermination",
            )
        return TerminationResult(False)

    def __repr__(self) -> str:
        return f"TextMentionTermination({self.keyword!r}, {self.source_field!r})"


# =====================================================================
# 默认条件工厂（向后兼容核心）
# =====================================================================


def default_termination() -> TerminationCondition:
    """默认终止条件 - 等价于现有 P4 行为

    等价于：MaxStepsTermination(MAX_STEPS) | StuckAgentTermination(STUCK_AGENT_REPEAT_LIMIT)

    MAX_STEPS 和 STUCK_AGENT_REPEAT_LIMIT 从 graph.py import 复用，
    保证常量单一来源。
    """
    # 延迟 import 避免循环依赖（graph.py 反向 import termination 的情况）
    from .graph import MAX_STEPS, STUCK_AGENT_REPEAT_LIMIT

    return MaxStepsTermination(MAX_STEPS) | StuckAgentTermination(
        STUCK_AGENT_REPEAT_LIMIT
    )
