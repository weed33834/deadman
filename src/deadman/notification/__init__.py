"""主动通知护栏模块 - 实现 notification-guardrails.md L4 规则

身后事场景下的主动推送是高风险动作（任何提醒都可能造成二次伤害），
本模块按 notification-guardrails.md 第七章要求实现 NotificationGuardrail 类。

所有 Cron / Gateway 主动推送代码路径必须先调 can_send()。
"""

from __future__ import annotations

from .guardrail import NotificationGuardrail

__all__ = ["NotificationGuardrail"]
