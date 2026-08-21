"""P4.1 Handoff 一等公民 - OpenAI Swarm 风格的 agent 转交机制

借鉴 OpenAI Swarm 的 handoff 设计，把"agent 转介"从简单的关键词匹配升级为
一等公民对象：携带 LLM 压缩后的消息历史、跨 agent 传递的 context_variables、
白名单/黑名单 filter_rules，让目标 agent 能无缝接续上下文。

核心组件：
- HandoffContext: 单次 handoff 的不可变快照（from/to/reason/压缩消息/上下文/过滤规则）
- HandoffManager: 创建/应用 handoff，封装 LLM 压缩 + 上下文过滤 + 降级路径

Feature flag: DEADMAN_HANDOFF_ENABLED=1 默认开启（P1-1 企业级落地）
- 开启时 LLM 不可用降级到 [:500] 截断，保持旧的兜底行为
- 显式关闭（DEADMAN_HANDOFF_ENABLED=0）时 create_handoff 返回 None，
  调用方走旧的 TransferSummary 截断路径；测试套件通过 conftest autouse
  fixture 默认关闭以保证隔离（test_handoff.py 显式 monkeypatch 开启）

降级路径全覆盖：
1. feature flag 关闭 → create_handoff 返回 None，调用方走旧路径
2. LLM 不可用（api_key 为空或抛异常）→ 压缩消息退化为 [:500] 截断
3. filter_rules 解析失败 → 透传全部 context_vars（最宽容降级）
4. apply_handoff 注入失败 → 不修改 target_state（无副作用）
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Feature flag - 默认开启（P1-1：企业级落地，handoff 作为一等公民默认启用）
# 测试套件通过 conftest.py 的 _disable_handoff_by_default autouse fixture
# 关闭以保证隔离；test_handoff.py 显式 monkeypatch 开启。
# =====================================================================
HANDOFF_ENABLED: bool = os.environ.get("DEADMAN_HANDOFF_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# LLM 不可用时的截断长度（对齐旧 TransferSummary 的 [:500] 行为）
HANDOFF_FALLBACK_TRUNCATE: int = int(os.environ.get("DEADMAN_HANDOFF_FALLBACK_TRUNCATE", "500"))


# =====================================================================
# 数据模型
# =====================================================================


@dataclass
class HandoffContext:
    """单次 handoff 的不可变快照

    封装 OpenAI Swarm 风格的 handoff 上下文：
    - compressed_message: LLM 把消息历史压缩为 2-3 句摘要，供目标 agent 快速接续
    - context_variables: 跨 agent 传递的上下文（user_profile / pending_items 等）
    - filter_rules: 决定哪些 context_vars 传递（白名单/黑名单键）

    filter_rules 约定（每条规则字符串）：
    - "allow:KEY" → 仅传递此 key（白名单）
    - "deny:KEY"  → 排除此 key（黑名单）
    - 若存在任何 allow 规则 → 仅传递 allow 列表中的 key（deny 仍生效作二次过滤）
    - 否则 → 传递全部 key，再排除 deny 列表
    """

    from_agent: str
    to_agent: str
    reason: str
    compressed_message: str
    context_variables: dict[str, Any] = field(default_factory=dict)
    filter_rules: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    transfer_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# =====================================================================
# HandoffManager
# =====================================================================


class HandoffManager:
    """Handoff 管理器 - 创建与应用 handoff 上下文

    所有方法均设计为可降级：
    - create_handoff: feature flag 关闭返回 None；LLM 不可用降级到截断
    - apply_handoff: 注入失败不抛异常，仅记录日志
    """

    def __init__(self, llm_client: Any | None = None):
        """Args:
        llm_client: 可选的 LLM 客户端（需支持 async chat_json）；
                    为 None 时延迟到调用点从 deadman.llm 取全局单例
        """
        self._llm_client = llm_client

    # ------------------------------------------------------------------
    # LLM 客户端解析
    # ------------------------------------------------------------------

    def _resolve_llm(self) -> Any | None:
        """解析 LLM 客户端 - 优先用构造时传入的，否则取全局单例

        返回 None 表示 LLM 不可用（调用方走降级路径）
        """
        if self._llm_client is not None:
            return self._llm_client
        try:
            from ..llm import llm_client

            if llm_client and llm_client.api_key:
                return llm_client
        except Exception as e:  # pragma: no cover - 防御性
            logger.debug("解析全局 llm_client 失败: %s", e)
        return None

    # ------------------------------------------------------------------
    # 消息历史压缩
    # ------------------------------------------------------------------

    async def _compress_message_history(self, message_history: list[str], llm: Any | None) -> str:
        """用 LLM 把消息历史压缩为 2-3 句摘要

        降级路径：
        - LLM 不可用 → 退化为拼接后 [:500] 截断（保持旧行为）
        - LLM 抛异常 → 同上降级
        - LLM 返回空 → 同上降级
        """
        # 拼接历史（每条消息一行）
        joined = "\n".join(str(m) for m in message_history if m)
        if not joined:
            return ""

        # LLM 不可用 → 直接截断降级
        if llm is None or not getattr(llm, "api_key", ""):
            return joined[:HANDOFF_FALLBACK_TRUNCATE]

        try:
            prompt = (
                "你是多智能体协作平台的上下文压缩器。"
                "请把以下对话历史压缩为 2-3 句摘要，"
                "保留关键事实、用户意图、已完成的步骤和待办事项，"
                "不要添加任何新信息或主观判断。\n\n"
                f"对话历史：\n{joined[:3000]}\n\n"
                "请直接输出摘要文本，不要 JSON 包装，不要前缀说明。"
            )
            # 走 chat_json 保持与计划文档一致；若客户端无 chat_json 走 chat
            if hasattr(llm, "chat_json"):
                try:
                    data = await llm.chat_json(
                        [{"role": "user", "content": prompt}],
                        temperature=0.2,
                    )
                    # 兼容 LLM 返回 {"summary": "..."} 或纯字符串
                    if isinstance(data, dict):
                        summary = (
                            data.get("summary")
                            or data.get("compressed_message")
                            or data.get("message")
                            or ""
                        )
                    else:
                        summary = str(data) if data else ""
                except Exception as e:
                    logger.debug("chat_json 压缩失败，回退 chat: %s", e)
                    summary = await llm.chat(
                        [{"role": "user", "content": prompt}],
                        temperature=0.2,
                    )
            else:
                summary = await llm.chat(
                    [{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
            summary = (summary or "").strip()
            if not summary:
                return joined[:HANDOFF_FALLBACK_TRUNCATE]
            return summary
        except Exception as e:
            logger.warning("LLM 压缩消息历史失败，降级到截断: %s", e)
            return joined[:HANDOFF_FALLBACK_TRUNCATE]

    # ------------------------------------------------------------------
    # 上下文过滤
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_filter_rules(
        rules: list[str] | None,
    ) -> tuple[set[str], set[str]]:
        """解析 filter_rules → (allow_set, deny_set)

        规则格式（每条字符串）：
        - "allow:KEY" / "+KEY" / "whitelist:KEY" → 加入 allow_set
        - "deny:KEY"  / "-KEY" / "blacklist:KEY" → 加入 deny_set
        - 未知格式忽略（不抛异常，最宽容降级）

        Returns:
            (allow_set, deny_set) - 都为空表示无过滤
        """
        if not rules:
            return set(), set()
        allow: set[str] = set()
        deny: set[str] = set()
        for raw in rules:
            if not isinstance(raw, str):
                continue
            rule = raw.strip()
            if not rule:
                continue
            # 统一前缀解析（支持 allow:/whitelist:/+ 和 deny:/blacklist:/-）
            lowered = rule.lower()
            key: str | None = None
            kind: str = ""
            for prefix in ("allow:", "whitelist:", "+"):
                if lowered.startswith(prefix):
                    kind = "allow"
                    key = rule[len(prefix) :].strip()
                    break
            if kind != "allow":
                for prefix in ("deny:", "blacklist:", "-"):
                    if lowered.startswith(prefix):
                        kind = "deny"
                        key = rule[len(prefix) :].strip()
                        break
            if kind and key:
                if kind == "allow":
                    allow.add(key)
                else:
                    deny.add(key)
            # else: 忽略未知格式
        return allow, deny

    @classmethod
    def _apply_filter_rules(
        cls,
        context_vars: dict[str, Any] | None,
        rules: list[str] | None,
    ) -> dict[str, Any]:
        """应用 filter_rules 过滤 context_vars

        语义：
        - 若 allow 集非空 → 仅传递 allow 列表中的 key（白名单）
        - 否则 → 传递全部 key
        - 最后从结果中剔除 deny 集合中的 key（黑名单二次过滤）
        - 解析失败/无规则 → 透传全部（最宽容降级）
        """
        if not context_vars:
            return {}
        allow, deny = cls._parse_filter_rules(rules)
        if allow:
            filtered = {k: v for k, v in context_vars.items() if k in allow}
        else:
            filtered = dict(context_vars)
        for k in deny:
            filtered.pop(k, None)
        return filtered

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def create_handoff(
        self,
        from_agent: str,
        to_agent: str,
        reason: str,
        message_history: list[str] | None,
        context_vars: dict[str, Any] | None = None,
        filter_rules: list[str] | None = None,
    ) -> HandoffContext | None:
        """创建一次 handoff 上下文

        Args:
            from_agent: 来源智能体名
            to_agent: 目标智能体名
            reason: 转交原因
            message_history: 待压缩的消息历史（list[str]，每条一条消息）
            context_vars: 跨 agent 传递的上下文变量
            filter_rules: 白名单/黑名单规则（见 _parse_filter_rules）

        Returns:
            HandoffContext 实例；feature flag 关闭时返回 None（调用方走旧路径）

        降级路径：
        1. HANDOFF_ENABLED=False → 返回 None
        2. LLM 不可用 → compressed_message 退化为 [:500] 截断
        3. filter_rules 解析失败 → 透传全部 context_vars
        """
        if not HANDOFF_ENABLED:
            logger.debug("handoff disabled (DEADMAN_HANDOFF_ENABLED=0), skip create_handoff")
            return None

        message_history = message_history or []
        context_vars = context_vars or {}

        # 压缩消息历史（LLM 不可用时内部降级到截断）
        llm = self._resolve_llm()
        compressed = await self._compress_message_history(message_history, llm)

        # 应用 filter_rules 过滤 context_vars
        filtered_vars = self._apply_filter_rules(context_vars, filter_rules)

        ctx = HandoffContext(
            from_agent=from_agent,
            to_agent=to_agent,
            reason=reason,
            compressed_message=compressed,
            context_variables=filtered_vars,
            filter_rules=list(filter_rules) if filter_rules else [],
        )
        logger.info(
            "handoff created: %s -> %s (reason=%s, ctx_keys=%d, compressed_len=%d)",
            from_agent,
            to_agent,
            reason,
            len(filtered_vars),
            len(compressed),
        )
        return ctx

    def apply_handoff(
        self,
        handoff: HandoffContext,
        target_state: dict[str, Any],
    ) -> None:
        """把 handoff 上下文注入到目标 agent 的 working state

        注入策略：
        - compressed_message → state["draft_response"] 前缀（如未占用）
        - context_variables → 合并到 state["user_profile"]（不覆盖已存在的 key）
        - 失败时仅记录日志，不修改 state（无副作用）

        Args:
            handoff: create_handoff 返回的 HandoffContext
            target_state: 目标 agent 的 ConversationState（in-place 修改）
        """
        if handoff is None:
            return
        try:
            # 注入 compressed_message 到 draft_response 前缀（不覆盖已有响应）
            existing = target_state.get("draft_response", "")
            if handoff.compressed_message and not existing:
                target_state["draft_response"] = (
                    f"[来自 {handoff.from_agent} 的交接上下文]\n{handoff.compressed_message}\n"
                )
            # 合并 context_variables 到 user_profile（不覆盖已有 key）
            profile = dict(target_state.get("user_profile", {}))
            for k, v in handoff.context_variables.items():
                profile.setdefault(k, v)
            target_state["user_profile"] = profile
            # 记录 handoff 元数据到 metrics
            metrics = dict(target_state.get("metrics", {}))
            metrics["handoff_from"] = handoff.from_agent
            metrics["handoff_to"] = handoff.to_agent
            metrics["handoff_transfer_id"] = handoff.transfer_id
            target_state["metrics"] = metrics
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning("apply_handoff 注入失败，state 不变: %s", e)
