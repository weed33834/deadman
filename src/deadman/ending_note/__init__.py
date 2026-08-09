"""终活笔记（エンディングノート）+ 家庭共享模块 - Phase 10

参考日本終活应用（わが家ノート/SouSou/そなえ/遺言ネット）设计，
结合 deadman 安全约束实现：

差异化（vs 竞品）：
    - AI 引导填写（vs 竞品纯表单） - 通过 EndingNoteGuide 对话引导
    - 明确不是法律文件（vs 部分竞品含糊） - 遵守 service-boundary-framework
    - PII 脱敏存储（vs 部分竞品明文） - 遵守 legal-compliance-framework PIPL 章节
    - 触发时机延后（vs 竞品实时配信） - 遵守 notification-guardrails
      （死亡确认 trigger 需 7 天等待期，避免情绪冲动决策）

模块组成：
    - models: EndingNote dataclass（9 章 + 共享 + 安全标记）
    - store:  EndingNoteStore（加密 + PII 脱敏 + 共享 + 投递触发）
    - guide:  EndingNoteGuide（AI 引导填写 + 安全信号检测）

合规关联（rules/）：
    - safety-protocol.md L0：检测自杀风险信号 → 立即停止流程引导
    - integrity-framework.md L1：不编造法律效力
    - legal-compliance-framework.md L3 PIPL：敏感 PII 加密 + 脱敏 + 单独同意
    - service-boundary-framework.md L3：明确告知"终活笔记不是法律文件"
    - notification-guardrails.md L4：投递触发不自动执行，需 7 天等待期
"""

from __future__ import annotations

from .guide import EndingNoteGuide
from .models import EndingNote
from .store import EndingNoteStore

__all__ = ["EndingNote", "EndingNoteGuide", "EndingNoteStore"]
