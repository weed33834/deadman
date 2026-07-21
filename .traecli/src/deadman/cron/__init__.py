"""deadman.cron - Cron 定时任务调度模块

借鉴 Hermes Agent (cron/scheduler.py, MIT License) 的设计，按身后事场景改造：
严格遵守 .traecli/rules/notification-guardrails.md 第三章约束。

核心差异（与 Hermes 相比）：
- 默认 enabled=false（Hermes 默认开启 heartbeat）
- 任务创建需双重确认（propose → 下一轮 confirm）
- 单用户任务上限 5 条（Hermes 无上限）
- 最小触发间隔 24 小时（Hermes 无限制）
- 最长持续 30 天（Hermes 无限制）
- 失败不自动重试（Hermes 有 retry）
- 不支持 heartbeat / scale_to_zero（deadman 轻量部署）
- 不依赖 croniter，自实现轻量解析器

所有触发动作必须先过 NotificationGuardrail.can_send()，
确保身后事场景的"默认静默、主动推送是特权"原则不被绕过。
"""

from .expr import CronExpr
from .scheduler import CronJob, CronScheduler

__all__ = ["CronExpr", "CronJob", "CronScheduler"]
