"""遗码通（Phase 13）

参考重庆"渝逝有安"遗码通：逝者唯一标识贯穿全流程。

重要：deadman 是信息引导平台，不存储逝者敏感 PII。
本模块仅用于：
    - 用户在多个智能体之间引用同一个逝者案例
    - 跨会话续接（"我父亲的事"指向同一个 case_id）
    - 不与任何官方系统对接

遵守：
    - rules/legal-compliance-framework.md 第五章 PIPL：不存敏感 PII
    - rules/integrity-framework.md：case_id 是内部 ID，不冒充官方编号
    - rules/service-boundary-framework.md：不与官方系统对接
    - rules/safety-protocol.md：涉及自杀/非正常死亡触发 L0

子模块：
    - registry: DecedentRegistry / DecedentRecord
"""

from __future__ import annotations

from .registry import DecedentRecord, DecedentRegistry

__all__ = ["DecedentRecord", "DecedentRegistry"]
