"""P8.27 AI 红线清单 - 代码级硬性 enforcement (HARD ENFORCEMENT)。

某些场景绝对禁止 AI 代办(代签遗嘱 / 代签合同 / 医疗决策 / 法律决策 /
资金转账 / 死亡认定 / 监护人指定),必须人工处理。

设计原则:
    - HARD ENFORCEMENT:代码级强制,不可被 prompt 绕过
      (与 prompt-level 软约束互补,代码层是底线)
    - 每条红线 rule 有:action category / allowed_contexts(空=永远禁止)/
      exception_role(只有 admin 角色可临时解锁)/ reason_text(中文说明)
    - 即便是 admin,也只允许在特定 context 下解锁(不空开)

借鉴:
    - Anthropic Responsible Scaling Policy
    - 中国《生成式人工智能服务管理暂行办法》第 4 条 (不得生成法律/行政法规禁止的内容)
    - EU AI Act Annex III (high-risk AI 须人工监督)

feature flag:`DEADMAN_GOVERNANCE_ENABLED=0` 关闭时仍 enforce(底线保护,
不能因为治理模块关闭就放开红线);但若 DEADMAN_AI_REDLINE_BYPASS=1
(仅限 CI 测试场景),允许绕过。生产环境绝不开此开关。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RedlineCategory(str, Enum):
    """AI 红线类别 - 7 个绝对禁止场景。

    借鉴 Anthropic Responsible Scaling Policy + deadman 业务场景。
    """

    SIGN_WILL = "sign_will"  # 代签遗嘱
    SIGN_CONTRACT = "sign_contract"  # 代签合同
    MEDICAL_DECISION = "medical_decision"  # 医疗决策
    LEGAL_DECISION = "legal_decision"  # 法律决策 (代理诉讼 / 调解)
    FINANCIAL_TRANSFER = "financial_transfer"  # 资金转账 / 财产处分
    DEATH_DETERMINATION = "death_determination"  # 死亡认定
    GUARDIAN_ASSIGNMENT = "guardian_assignment"  # 监护人指定


class RedlineViolation(Exception):
    """红线违规异常 - 当 enforce(action) 时若不允许则抛此异常。

    Attributes:
        category: 触发的红线类别
        action: 触发的动作描述
        reason: 中文说明 (来自 reason_text)
        context: 上下文 (dict)
    """

    def __init__(
        self,
        category: RedlineCategory,
        action: str,
        reason: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        self.category = category
        self.action = action
        self.reason = reason
        self.context = context or {}
        super().__init__(
            f"AI 红线违规: action={action!r} category={category.value} reason={reason}"
        )


@dataclass
class RedlineRule:
    """单条红线规则。

    Attributes:
        category: 红线类别
        action_keywords: 触发关键词 / 动作描述 (用于匹配 action)
        allowed_contexts: 允许的上下文条件 (空 = 永远禁止)
            例:["admin_override", "audit_required"]
        exception_role: 可临时解锁的角色 (admin)
        reason_text: 中文说明 (用户可见)
        requires_human: 是否必须人工处理 (默认 True)
    """

    category: RedlineCategory
    action_keywords: list[str]
    allowed_contexts: list[str] = field(default_factory=list)
    exception_role: str = "admin"
    reason_text: str = ""
    requires_human: bool = True


@dataclass
class RedlineResult:
    """红线检查结果。"""

    allowed: bool
    category: Optional[RedlineCategory]
    reason: str
    requires_human: bool = True
    matched_rule: Optional[RedlineRule] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "category": self.category.value if self.category else None,
            "reason": self.reason,
            "requires_human": self.requires_human,
            "matched_rule": self.matched_rule.category.value if self.matched_rule else None,
        }


# =====================================================================
# 硬编码红线规则 - CODE-LEVEL ENFORCEMENT
# 不可被 prompt / 配置 / flag 绕过 (除 DEADMAN_AI_REDLINE_BYPASS 测试开关)
# =====================================================================
_HARDCODED_RULES: list[RedlineRule] = [
    RedlineRule(
        category=RedlineCategory.SIGN_WILL,
        action_keywords=["sign_will", "代签遗嘱", "代书遗嘱", "公证遗嘱"],
        allowed_contexts=[],  # 永远禁止 AI 代签
        reason_text="遗嘱必须本人亲签或依法代书,AI 不得代签 (民法典第 1134-1139 条)",
    ),
    RedlineRule(
        category=RedlineCategory.SIGN_CONTRACT,
        action_keywords=["sign_contract", "代签合同", "代为签约", "代理签字"],
        allowed_contexts=[],
        reason_text="合同签署涉及民事法律行为效力,AI 无民事行为能力,不得代签",
    ),
    RedlineRule(
        category=RedlineCategory.MEDICAL_DECISION,
        action_keywords=[
            "medical_decision", "医疗决策", "诊断", "开具处方",
            "治疗方案", "停药", "手术决定",
        ],
        allowed_contexts=[],  # 涉及生命健康,绝对禁止 AI 决策
        reason_text="医疗决策涉及生命健康,必须由执业医师决定 (执业医师法)",
    ),
    RedlineRule(
        category=RedlineCategory.LEGAL_DECISION,
        action_keywords=[
            "legal_decision", "代理诉讼", "出庭", "法律意见书",
            "诉讼策略决定",
        ],
        allowed_contexts=["advisory_only"],  # 仅允许咨询性意见
        exception_role="admin",
        reason_text="法律决策须由执业律师 / 法院作出,AI 仅可提供参考性信息",
    ),
    RedlineRule(
        category=RedlineCategory.FINANCIAL_TRANSFER,
        action_keywords=[
            "financial_transfer", "转账", "汇款", "财产处分",
            "继承财产分配", "资金转移",
        ],
        allowed_contexts=["admin_audit"],  # 仅 admin 审计场景
        exception_role="admin",
        reason_text="资金转账涉及财产权处分,需本人或法定代理人亲为,AI 不得代办",
    ),
    RedlineRule(
        category=RedlineCategory.DEATH_DETERMINATION,
        action_keywords=["death_determination", "死亡认定", "宣告死亡", "死亡时间判定"],
        allowed_contexts=[],  # 绝对禁止
        reason_text="死亡认定须由医疗机构 / 法院依法作出,AI 不得认定 (民法典第 46 条)",
    ),
    RedlineRule(
        category=RedlineCategory.GUARDIAN_ASSIGNMENT,
        action_keywords=["guardian_assignment", "监护人指定", "监护权变更", "委托监护"],
        allowed_contexts=[],
        reason_text="监护人指定涉及人身监护权,须由法院 / 民政部门指定 (民法典第 31-36 条)",
    ),
]

# 关键词 → 规则的反向索引(便于 O(1) 匹配)
_KEYWORD_INDEX: dict[str, RedlineRule] = {}
for _r in _HARDCODED_RULES:
    for _kw in _r.action_keywords:
        _KEYWORD_INDEX[_kw] = _r


class AIRedline:
    """AI 红线 enforcement 入口 - HARD CODED。

    用法:
        redline = get_ai_redline()
        result = redline.is_allowed("代签遗嘱", context={"role": "user"})
        if not result.allowed:
            raise RedlineViolation(...)
        # 或直接 enforce (不允许时抛异常)
        redline.enforce("代签遗嘱", context={...})  # raises RedlineViolation
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 拷贝硬编码规则到实例(便于测试 monkeypatch)
        self._rules: list[RedlineRule] = list(_HARDCODED_RULES)

    def list_redlines(self) -> list[RedlineRule]:
        """列出所有红线规则。"""
        with self._lock:
            return list(self._rules)

    def is_allowed(
        self,
        action: str,
        context: Optional[dict[str, Any]] = None,
    ) -> RedlineResult:
        """检查 action 是否被红线禁止。

        匹配逻辑:
            1. 测试 bypass (DEADMAN_AI_REDLINE_BYPASS=1) → 允许
            2. action 文本匹配任一关键词 → 命中规则
            3. 命中后检查 allowed_contexts:
               - 空 allowed_contexts → 永远禁止
               - context 满足任一 allowed_contexts 值 → 允许(若角色允许)
            4. 未命中任何规则 → 允许
        """
        ctx = context or {}

        # 1. 测试 bypass (生产环境绝不开启)
        if os.environ.get("DEADMAN_AI_REDLINE_BYPASS", "0").lower() in ("1", "true", "yes", "on"):
            logger.warning("AI redline BYPASS enabled (test only), allowing: %s", action)
            return RedlineResult(
                allowed=True,
                category=None,
                reason="bypass_enabled (test only)",
                requires_human=False,
            )

        # 2. 匹配规则
        matched = self._match_rule(action)
        if matched is None:
            return RedlineResult(
                allowed=True,
                category=None,
                reason="no redline matched",
                requires_human=False,
            )

        # 3. 检查 allowed_contexts
        if matched.allowed_contexts:
            # 检查 context 是否满足任一 allowed_contexts
            ctx_satisfied = any(
                self._context_matches(ctx, ac) for ac in matched.allowed_contexts
            )
            if ctx_satisfied:
                # 进一步检查 exception_role
                role = ctx.get("role", "user")
                if role == matched.exception_role or matched.exception_role == "":
                    return RedlineResult(
                        allowed=True,
                        category=matched.category,
                        reason=f"allowed by exception_role={role}",
                        requires_human=matched.requires_human,
                        matched_rule=matched,
                    )
                else:
                    return RedlineResult(
                        allowed=False,
                        category=matched.category,
                        reason=f"role={role} 无权限解锁,需 {matched.exception_role}",
                        requires_human=True,
                        matched_rule=matched,
                    )
            else:
                return RedlineResult(
                    allowed=False,
                    category=matched.category,
                    reason=matched.reason_text,
                    requires_human=True,
                    matched_rule=matched,
                )

        # 4. allowed_contexts 为空 → 永远禁止
        return RedlineResult(
            allowed=False,
            category=matched.category,
            reason=matched.reason_text,
            requires_human=True,
            matched_rule=matched,
        )

    def enforce(
        self,
        action: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        """强制执行 - 若不允许则抛 RedlineViolation。

        Critical:这是 HARD ENFORCEMENT 入口,所有 critical 路径必须调用。
        """
        result = self.is_allowed(action, context)
        if not result.allowed:
            logger.warning(
                "AI redline enforced: action=%r category=%s reason=%s",
                action,
                result.category.value if result.category else None,
                result.reason,
            )
            raise RedlineViolation(
                category=result.category or RedlineCategory.SIGN_WILL,
                action=action,
                reason=result.reason,
                context=context,
            )

    # ==================================================================
    # 内部
    # ==================================================================

    def _match_rule(self, action: str) -> Optional[RedlineRule]:
        """匹配 action 到红线规则 (关键词子串匹配)。

        匹配优先级:精确匹配 > 子串匹配。
        """
        if not action:
            return None
        action_lower = action.lower()
        # 精确匹配
        with self._lock:
            for rule in self._rules:
                for kw in rule.action_keywords:
                    if kw == action or kw == action_lower:
                        return rule
            # 子串匹配
            for rule in self._rules:
                for kw in rule.action_keywords:
                    if kw in action or kw in action_lower:
                        return rule
        return None

    @staticmethod
    def _context_matches(ctx: dict[str, Any], allowed_context: str) -> bool:
        """检查 context 是否满足 allowed_context。

        约定:allowed_context 是 context 中的 key,且值为 True。
        例:allowed_context="admin_override",ctx={"admin_override": True}
        """
        return bool(ctx.get(allowed_context, False))


# =====================================================================
# 全局单例
# =====================================================================
_arl_instance: Optional[AIRedline] = None
_arl_lock = threading.Lock()


def get_ai_redline() -> AIRedline:
    global _arl_instance
    if _arl_instance is None:
        with _arl_lock:
            if _arl_instance is None:
                _arl_instance = AIRedline()
    return _arl_instance


def reset_ai_redline() -> None:
    """测试用:重置全局单例。"""
    global _arl_instance
    with _arl_lock:
        _arl_instance = None
