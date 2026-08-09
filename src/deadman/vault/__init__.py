"""数字遗产保险库（Phase 11）

提供加密存储 + 受益人指定 + 投递触发能力。
遵守 PIPL（加密 + 数据最小化）、notification-guardrails（on_death 7 天等待 + 受益人确认）。

子模块：
    - store: VaultStore / VaultItem

参见 rules/legal-compliance-framework.md 第五章 PIPL 特别条款。
参见 rules/notification-guardrails.md 第二章约束 1（显式 opt-in）。
"""

from __future__ import annotations

from .store import VaultItem, VaultStore

__all__ = ["VaultItem", "VaultStore"]
